import {
  acknowledgeSampleBatch,
  createProtocolId,
  DIGITAL_TWIN_CAPABILITY,
  DIGITAL_TWIN_MESSAGE,
  isHostMessageEvent,
  isRecord,
  isValidAppMessage,
  isValidAppReadyMessage,
  isValidProtocolId,
  makeProtocolMessage,
  postToHost,
} from './index.js'

function createClientError(message, code) {
  const error = new Error(message)
  if (code) error.code = code
  return error
}

export function createDigitalTwinAppClient({
  onHostReady,
  onSample,
  onSamples,
  onReset,
  onSimulationState,
  onResultsComplete,
  onPlaybackRate,
  onRunAccepted,
  onRunRejected,
  onRunCancelled,
  onError,
  capabilities = [],
  defaultPlaybackRate,
  playbackControlPosition,
  runRequestTimeoutMs = 30_000,
} = {}) {
  if (typeof window === 'undefined') {
    throw new Error('The digital twin app client can only be created in a browser.')
  }

  let disposed = false
  let hostReady = false
  let sceneReady = false
  let readySent = false
  let startupError = null
  let activeRunId = null
  let reception = createReception()
  const pendingRequests = new Map()
  const appCapabilities = new Set(Array.isArray(capabilities)
    ? capabilities.filter((capability) => (
      isValidAppReadyMessage(makeProtocolMessage(DIGITAL_TWIN_MESSAGE.APP_READY, { capabilities: [capability] }))
      && !Object.values(DIGITAL_TWIN_CAPABILITY).includes(capability)
    )).slice(0, 30)
    : [])
  if (typeof onPlaybackRate === 'function') appCapabilities.add(DIGITAL_TWIN_CAPABILITY.PLAYBACK_RATE)
  if (typeof onSamples === 'function' || typeof onSample === 'function') {
    appCapabilities.add(DIGITAL_TWIN_CAPABILITY.SAMPLE_ACKNOWLEDGEMENT)
  }
  const requestTimeoutMs = Number.isFinite(runRequestTimeoutMs) && runRequestTimeoutMs > 0
    ? runRequestTimeoutMs
    : 30_000

  function reportCallbackError(error) {
    if (typeof onError !== 'function') return
    try {
      onError(error instanceof Error ? error : new Error(String(error)))
    } catch {
      // An application error handler must not break the bridge listener.
    }
  }

  function invoke(callback, ...args) {
    if (typeof callback !== 'function') return undefined
    try {
      return callback(...args)
    } catch (error) {
      reportCallbackError(error)
      return undefined
    }
  }

  function createReception() {
    return { controller: new AbortController(), queue: Promise.resolve(), failed: false }
  }

  function resetReception(runId) {
    reception.controller.abort()
    reception = createReception()
    activeRunId = runId
  }

  function processInOrder(callback) {
    const current = reception
    const context = { runId: activeRunId ?? undefined, signal: current.controller.signal }
    current.queue = current.queue.then(async () => {
      if (context.signal.aborted || current.failed) return
      await callback(context)
    }).catch((error) => {
      if (context.signal.aborted) return
      current.failed = true
      reportCallbackError(error)
    })
  }

  function isCurrentRun(message) {
    return !message.runId || message.runId === activeRunId
  }

  function sendReadyIfPossible() {
    if (disposed || !hostReady || !sceneReady || readySent || startupError) return false
    const payload = {
      capabilities: [...appCapabilities],
      ...(Number.isFinite(defaultPlaybackRate) && defaultPlaybackRate > 0 && defaultPlaybackRate <= 100_000
        ? { defaultPlaybackRate }
        : {}),
      ...(['top-left', 'top-right', 'bottom-left', 'bottom-right'].includes(playbackControlPosition)
        ? { playbackControlPosition }
        : {}),
    }
    readySent = postToHost(DIGITAL_TWIN_MESSAGE.APP_READY, payload)
    return readySent
  }

  function sendStartupErrorIfPossible() {
    if (disposed || !hostReady || !startupError || readySent) return false
    readySent = postToHost(DIGITAL_TWIN_MESSAGE.APP_ERROR, { code: startupError })
    return readySent
  }

  function clearPendingRequest(requestId, error, result) {
    const pending = pendingRequests.get(requestId)
    if (!pending) return null
    window.clearTimeout(pending.timeout)
    pendingRequests.delete(requestId)
    if (error) pending.reject(error)
    else pending.resolve(result)
    return pending
  }

  function handleHostMessage(event) {
    if (disposed || !isHostMessageEvent(event)) return
    const message = event.data

    if (message.type === DIGITAL_TWIN_MESSAGE.HOST_READY) {
      if (!hostReady) {
        hostReady = true
        invoke(onHostReady, message.extensions || {})
      }
      if (startupError) sendStartupErrorIfPossible()
      else sendReadyIfPossible()
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.RUN_ACCEPTED) {
      const pending = clearPendingRequest(message.requestId, null, {
        requestId: message.requestId,
        runId: message.runId,
      })
      if (!pending) return
      if (activeRunId !== message.runId) resetReception(message.runId)
      invoke(onRunAccepted, { requestId: message.requestId, runId: message.runId })
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.RUN_REJECTED) {
      const error = createClientError('Marketplace rejected the simulation request.', message.code)
      const pending = message.requestId
        ? clearPendingRequest(message.requestId, error)
        : null
      if (pending || !message.requestId) invoke(onRunRejected, { requestId: message.requestId, code: message.code })
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.RUN_CANCELLED) {
      if (message.runId !== activeRunId) return
      invoke(onRunCancelled, message.runId)
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.SAMPLE) {
      if (!isCurrentRun(message)) return
      processInOrder(async (context) => {
        await onSample?.({ time: message.time, outputs: message.outputs }, context)
      })
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.SAMPLES) {
      if (!isCurrentRun(message)) return
      processInOrder(async (context) => {
        if (typeof onSamples === 'function') {
          await onSamples(message.samples, message, context)
        } else if (typeof onSample === 'function') {
          for (const sample of message.samples) {
            if (context.signal.aborted) return
            await onSample(sample, context)
          }
        }
        if (!context.signal.aborted) acknowledgeSampleBatch(message)
      })
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.RESULTS_COMPLETE) {
      if (!isCurrentRun(message)) return
      processInOrder(async (context) => {
        await onResultsComplete?.(message, context)
      })
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.RESET) {
      resetReception(message.runId ?? null)
      invoke(onReset, message.runId ?? null)
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.SIMULATION_STATE) {
      if (!isCurrentRun(message)) return
      invoke(onSimulationState, message.state, message)
      return
    }

    if (message.type === DIGITAL_TWIN_MESSAGE.PLAYBACK_CONTROL) {
      invoke(onPlaybackRate, message.rate, message)
    }
  }

  window.addEventListener('message', handleHostMessage)

  function reportReady() {
    if (disposed || startupError) return false
    sceneReady = true
    return sendReadyIfPossible()
  }

  function reportError(code = 'APP_STARTUP_FAILED') {
    if (disposed || readySent) return false
    const normalizedCode = typeof code === 'string' && code.length > 0
      ? code.slice(0, 128)
      : 'APP_STARTUP_FAILED'
    startupError = normalizedCode
    return sendStartupErrorIfPossible()
  }

  function run(request = {}) {
    if (disposed) return Promise.reject(createClientError('The digital twin app client has been disposed.'))
    if (!hostReady || !readySent || startupError) {
      return Promise.reject(createClientError('The Marketplace host is not ready to run the FMU.'))
    }
    if (!isRecord(request)) return Promise.reject(createClientError('The FMU run request is invalid.'))

    const { parameters = {}, options = {} } = request

    const requestId = createProtocolId('request')
    const message = makeProtocolMessage(DIGITAL_TWIN_MESSAGE.RUN_REQUEST, { requestId, parameters, options })
    if (!isValidAppMessage(message)) return Promise.reject(createClientError('The FMU run request is invalid.'))

    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        clearPendingRequest(requestId, createClientError('Marketplace did not answer the FMU run request.', 'REQUEST_TIMEOUT'))
      }, requestTimeoutMs)
      pendingRequests.set(requestId, { resolve, reject, timeout })
      try {
        if (!postToHost(DIGITAL_TWIN_MESSAGE.RUN_REQUEST, { requestId, parameters, options })) {
          clearPendingRequest(requestId, createClientError('The digital twin app is not hosted by Marketplace.'))
        }
      } catch (error) {
        clearPendingRequest(requestId, error)
      }
    })
  }

  function cancel(runId = activeRunId) {
    if (disposed || !isValidProtocolId(runId)) return false
    return postToHost(DIGITAL_TWIN_MESSAGE.RUN_CANCEL, { runId })
  }

  function requestModelChange(modelId) {
    if (disposed) return false
    const message = makeProtocolMessage(DIGITAL_TWIN_MESSAGE.MODEL_CHANGE, { modelId })
    if (!isValidAppMessage(message)) return false
    return postToHost(DIGITAL_TWIN_MESSAGE.MODEL_CHANGE, { modelId })
  }

  function dispose() {
    if (disposed) return
    disposed = true
    reception.controller.abort()
    window.removeEventListener('message', handleHostMessage)
    for (const requestId of pendingRequests.keys()) {
      clearPendingRequest(requestId, createClientError('The digital twin app client has been disposed.'))
    }
  }

  return {
    reportReady,
    reportError,
    run,
    cancel,
    requestModelChange,
    dispose,
    get hostReady() { return hostReady },
    get activeRunId() { return activeRunId },
  }
}
