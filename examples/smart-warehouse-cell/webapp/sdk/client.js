import {
  createProtocolId, DIGITAL_TWIN_CAPABILITY, DIGITAL_TWIN_MESSAGE as MESSAGE,
  isHostMessageEvent, isValidOptions, isValidParameters, isValidInputSchedule,
  isValidSample, postToHost, MAX_SAMPLES_PER_BATCH,
} from './index.js'
import { connectFmuSession } from './fmu-client.js'
import {
  createAdaptiveBatchController,
  getAdaptiveBatchSize,
  observeAdaptiveBatch,
} from './batch-controller.js'

const errorWithCode = (message, code) => Object.assign(new Error(message), { code })
const wait = (ms, signal) => new Promise((resolve, reject) => {
  const aborted = () => { clearTimeout(timer); reject(signal.reason) }
  const timer = setTimeout(() => { signal.removeEventListener('abort', aborted); resolve() }, ms)
  signal.addEventListener('abort', aborted, { once: true })
  if (signal.aborted) aborted()
})

/** Optional host UI integration composed with the independent direct FMU client. */
export function createDigitalTwinAppClient({
  getConnection, onHostReady, onSample, onSamples, onReset, onSimulationState,
  onBatchRendered,
  onResultsComplete, onPlaybackRate, onRunAccepted, onRunRejected, onRunCancelled, onError,
  defaultPlaybackRate, playbackControlPosition, runRequestTimeoutMs = 30_000,
} = {}) {
  let disposed = false
  let hostReady = typeof getConnection === 'function'
  let sceneReady = false
  let readySent = false
  let startupError
  let active = null
  const grants = new Map()
  const capabilities = [DIGITAL_TWIN_CAPABILITY.EXECUTION_CONTROL]
  if (typeof onPlaybackRate === 'function') capabilities.push(DIGITAL_TWIN_CAPABILITY.PLAYBACK_RATE)
  const report = (error) => { try { onError?.(error) } catch { /* The app owns its error presentation. */ } }

  function sendReady() {
    if (!disposed && hostReady && sceneReady && !readySent && !startupError) {
      readySent = postToHost(MESSAGE.APP_READY, {
        capabilities,
        ...(defaultPlaybackRate === undefined ? {} : { defaultPlaybackRate }),
        ...(playbackControlPosition === undefined ? {} : { playbackControlPosition }),
      })
    }
  }
  function finishGrant(requestId, error, value) {
    const pending = grants.get(requestId)
    if (!pending) return
    grants.delete(requestId)
    clearTimeout(pending.timer)
    pending.signal.removeEventListener('abort', pending.abort)
    if (error) pending.reject(error)
    else pending.resolve(value)
  }
  function receive(event) {
    if (disposed || !isHostMessageEvent(event)) return
    const message = event.data
    if (message.type === MESSAGE.HOST_READY) {
      if (!hostReady) { hostReady = true; try { onHostReady?.(message.extensions || {}) } catch (error) { report(error) } }
      readySent = false
      if (startupError) postToHost(MESSAGE.APP_ERROR, { code: startupError })
      else sendReady()
    } else if (message.type === MESSAGE.CONNECTION_READY) {
      finishGrant(message.requestId, null, { connection: message.connection, inputSchedule: message.inputSchedule || [] })
    } else if (message.type === MESSAGE.CONNECTION_ERROR) {
      finishGrant(message.requestId, errorWithCode('The provider connection could not be authorized.', message.code))
    } else if (message.type === MESSAGE.PLAYBACK_CONTROL) {
      try { onPlaybackRate?.(message.rate, message) } catch (error) { report(error) }
    } else if (message.type === MESSAGE.EXECUTION_CANCEL && active?.runId === message.runId) cancel(message.runId)
  }
  globalThis.window?.addEventListener('message', receive)
  // Async apps (e.g. a React app loading its config) may mount after iframe load.
  postToHost(MESSAGE.APP_HELLO)
  if (getConnection) queueMicrotask(() => { if (!disposed) { try { onHostReady?.({}) } catch (error) { report(error) } } })

  function requestConnection(options, signal) {
    if (getConnection) return Promise.resolve(getConnection({ options, signal })).then((grant) => grant.connection ? grant : { connection: grant, inputSchedule: [] })
    if (!hostReady || !readySent) return Promise.reject(errorWithCode('The host is not ready to authorize an FMU connection.', 'HOST_NOT_READY'))
    const requestId = createProtocolId('grant')
    return new Promise((resolve, reject) => {
      const abort = () => finishGrant(requestId, signal.reason)
      const timer = setTimeout(() => finishGrant(requestId, errorWithCode('The connection grant timed out.', 'REQUEST_TIMEOUT')), runRequestTimeoutMs)
      grants.set(requestId, { resolve, reject, timer, signal, abort })
      signal.addEventListener('abort', abort, { once: true })
      postToHost(MESSAGE.CONNECTION_REQUEST, { requestId, options })
    })
  }

  function state(run, value) {
    if (disposed) return
    try { onSimulationState?.(value, { runId: run.runId, state: value }) } catch (error) { report(error) }
    postToHost(MESSAGE.EXECUTION_STATE, { runId: run.runId, state: value, options: run.options })
  }
  async function deliver(run, samples) {
    run.controller.signal.throwIfAborted()
    if (!samples.length || samples.length > MAX_SAMPLES_PER_BATCH || !samples.every(isValidSample)) throw errorWithCode('Invalid FMU samples.', 'INVALID_FMU_RESPONSE')
    const context = { runId: run.runId, signal: run.controller.signal }
    const batch = { runId: run.runId, samples, firstSampleIndex: run.sampleCount, lastSampleIndex: run.sampleCount + samples.length - 1 }
    if (onSamples) await onSamples(samples, batch, context)
    else for (const sample of samples) { context.signal.throwIfAborted(); await onSample?.(sample, context) }
    run.controller.signal.throwIfAborted()
    run.sampleCount += samples.length
    return batch
  }
  async function operation(run, type, expected, payload) {
    const readsOutputs = ['sim.getOutputs', 'sim.advance'].includes(type)
    const tracksAdaptiveResponse = run.batchPolicy === 'adaptive' && type === 'sim.advance'
    let requestResponseMs = 0
    while (true) {
      run.controller.signal.throwIfAborted()
      let requestStartedAt
      try {
        if (readsOutputs && run.nextOutputAt > performance.now()) await wait(Math.ceil(run.nextOutputAt - performance.now()), run.controller.signal)
        requestStartedAt = performance.now()
        const response = await run.session.request(type, expected, payload)
        if (tracksAdaptiveResponse) requestResponseMs += performance.now() - requestStartedAt
        if (readsOutputs) {
          if (tracksAdaptiveResponse) run.lastOutputResponseMs = requestResponseMs
          run.nextOutputAt = performance.now() + run.outputIntervalMs
        }
        return response
      }
      catch (error) {
        if (tracksAdaptiveResponse && requestStartedAt !== undefined) requestResponseMs += performance.now() - requestStartedAt
        if (tracksAdaptiveResponse) run.lastOutputResponseMs = requestResponseMs
        if (error.code !== 'OUTPUT_READ_RATE_LIMITED' || !Number.isFinite(error.retryAfterMs) || error.retryAfterMs < 1 || error.retryAfterMs > 10_000) throw error
        await wait(error.retryAfterMs, run.controller.signal)
      }
    }
  }
  async function finish(run, outcome, error) {
    if (run.finished) return
    run.finished = true
    run.session?.close()
    if (disposed) { if (active === run) active = null; return }
    if (error && outcome === 'error') report(error)
    state(run, outcome)
    if (outcome === 'cancelled') { try { onRunCancelled?.(run.runId) } catch (callbackError) { report(callbackError) } }
    try {
      await onResultsComplete?.({ runId: run.runId, state: outcome, sampleCount: run.sampleCount }, { runId: run.runId, signal: run.controller.signal })
    } catch (callbackError) { report(callbackError) }
    finally { if (active === run) active = null }
  }
  async function updateController(run, sample) {
    if (!run.inputController) return
    const values = await run.inputController(sample, { runId: run.runId, signal: run.controller.signal })
    run.controller.signal.throwIfAborted()
    if (values && Object.keys(values).length) {
      if (!isValidParameters(values)) throw errorWithCode('Invalid controller inputs.', 'INVALID_INPUT')
      await operation(run, 'sim.setInputs', 'sim.inputs.updated', { values })
    }
  }
  async function pump(run) {
    try {
      let time = run.options.startTime
      let eventIndex = 0
      while (time < run.options.stopTime - 1e-12) {
        run.controller.signal.throwIfAborted()
        while (eventIndex < run.schedule.length && run.schedule[eventIndex].time <= time + 1e-12) {
          await operation(run, 'sim.setInputs', 'sim.inputs.updated', { values: run.schedule[eventIndex++].values })
        }
        const targetTime = Math.min(run.options.stopTime, run.schedule[eventIndex]?.time ?? Infinity)
        const maxSamples = run.batchPolicy === 'adaptive'
          ? getAdaptiveBatchSize(run.batchController)
          : run.inputController ? 1 : run.maxBatchSamples
        const result = await operation(run, 'sim.advance', 'sim.samples', { targetTime, maxSamples, stepSize: run.options.stepSize })
        const batchReceivedAt = run.batchPolicy === 'adaptive' ? performance.now() : null
        if (!Array.isArray(result.samples) || !result.samples.length || !Number.isFinite(result.simTime) || result.simTime <= time || result.simTime > targetTime + 1e-9) throw errorWithCode('The FMU did not advance as requested.', 'INVALID_FMU_RESPONSE')
        let previous = time
        for (const sample of result.samples) {
          if (!Number.isFinite(sample.time) || sample.time <= previous || sample.time > result.simTime) throw errorWithCode('The FMU returned an unordered timeline.', 'INVALID_FMU_RESPONSE')
          previous = sample.time
        }
        if (previous !== result.simTime) throw errorWithCode('The FMU returned an incomplete timeline.', 'INVALID_FMU_RESPONSE')
        const batch = await deliver(run, result.samples)
        time = result.simTime
        if (run.batchPolicy === 'adaptive') {
          await onBatchRendered?.(result.samples, batch, { runId: run.runId, signal: run.controller.signal })
          run.batchController = observeAdaptiveBatch(run.batchController, {
            responseMs: run.lastOutputResponseMs,
            consumerDelayMs: performance.now() - batchReceivedAt,
          })
        }
        if (result.finished) break
        await updateController(run, result.samples.at(-1))
      }
      await finish(run, 'completed')
    } catch (error) { await finish(run, run.controller.signal.aborted ? 'cancelled' : 'error', error) }
  }

  async function run({ parameters = {}, options = {}, inputSchedule, inputController, batchPolicy = 'fixed', maxBatchSamples = MAX_SAMPLES_PER_BATCH } = {}) {
    if (disposed || active) throw errorWithCode('The app cannot start another FMU run.', 'RUN_UNAVAILABLE')
    if (!isValidParameters(parameters) || !isValidOptions(options)) throw errorWithCode('Invalid FMU run parameters.', 'INVALID_RUN_REQUEST')
    parameters = { ...parameters }
    options = { ...options }
    if (inputController !== undefined && typeof inputController !== 'function') throw errorWithCode('Invalid input controller.', 'INVALID_INPUT')
    if (!['fixed', 'adaptive'].includes(batchPolicy)) throw errorWithCode('Invalid batch policy.', 'INVALID_INPUT')
    if (!Number.isInteger(maxBatchSamples) || maxBatchSamples < 1 || maxBatchSamples > MAX_SAMPLES_PER_BATCH) throw errorWithCode(`maxBatchSamples must be an integer from 1 to ${MAX_SAMPLES_PER_BATCH}.`, 'INVALID_INPUT')
    if (batchPolicy === 'adaptive' && inputController) throw errorWithCode('Adaptive batching cannot be combined with a per-sample input controller.', 'INVALID_INPUT')
    if (inputController && maxBatchSamples !== MAX_SAMPLES_PER_BATCH && maxBatchSamples !== 1) throw errorWithCode('maxBatchSamples must be 1 when using a per-sample input controller.', 'INVALID_INPUT')
    const current = { runId: createProtocolId('run'), controller: new AbortController(), sampleCount: 0, options, inputController, batchPolicy, maxBatchSamples }
    active = current
    try {
      const grant = await requestConnection(options, current.controller.signal)
      current.controller.signal.throwIfAborted()
      current.session = await connectFmuSession(grant.connection, { signal: current.controller.signal })
      if (current.session.capabilities.advanceSamples !== true) throw errorWithCode('The provider FMU runtime needs batched WebSocket support.', 'UNSUPPORTED_FMU_RUNTIME')
      const outputHz = current.session.capabilities.maxOutputHz
      current.outputIntervalMs = Number.isFinite(outputHz) && outputHz >= 1 && outputHz <= 1000 ? 1000 / outputHz : 0
      const model = await current.session.request('model.describe', 'model.description', { includeVariables: false })
      current.options = {
        startTime: options.startTime ?? model.defaultStartTime ?? 0,
        stopTime: options.stopTime ?? model.defaultStopTime ?? 1,
        stepSize: options.stepSize ?? model.defaultStepSize ?? 0.001,
      }
      const { startTime, stopTime, stepSize } = current.options
      if (!isValidOptions(current.options) || stopTime <= startTime || stepSize <= 0) throw errorWithCode('Invalid simulation interval.', 'INVALID_RUN_REQUEST')
      if (batchPolicy === 'adaptive') current.batchController = createAdaptiveBatchController({ startTime, stopTime, stepSize, maxSamples: maxBatchSamples })
      const schedule = inputSchedule ?? grant.inputSchedule ?? []
      if (!isValidInputSchedule(schedule) || schedule.some((event) => event.time < startTime || event.time > stopTime)) throw errorWithCode('Invalid input schedule.', 'INVALID_INPUT_SCHEDULE')
      current.schedule = [...schedule].sort((a, b) => a.time - b.time)
      await current.session.request('sim.initialize', 'sim.state', { parameters, options: { ...current.options, timeMode: 'simtime' } })
      onReset?.(current.runId)
      state(current, 'running')
      const initial = await operation(current, 'sim.getOutputs', 'sim.outputs', {})
      const initialSample = { time: initial.simTime, outputs: initial.values }
      if (onSamples && onSample) await onSample(initialSample, { runId: current.runId, signal: current.controller.signal })
      await deliver(current, [initialSample])
      await updateController(current, initialSample)
      onRunAccepted?.({ runId: current.runId })
      void pump(current).catch(report)
      return { runId: current.runId }
    } catch (error) {
      await finish(current, current.controller.signal.aborted ? 'cancelled' : 'error', error)
      onRunRejected?.({ code: error.code || 'FMU_CONNECTION_FAILED' })
      throw error
    }
  }
  function cancel(runId = active?.runId) {
    if (!active || runId !== active.runId) return false
    active.controller.abort(new DOMException('Cancelled', 'AbortError'))
    return true
  }
  return {
    reportReady() { sceneReady = true; sendReady(); return typeof getConnection === 'function' || readySent },
    reportError(code = 'APP_STARTUP_FAILED') { startupError = code; return hostReady && postToHost(MESSAGE.APP_ERROR, { code }) },
    run, cancel,
    requestModelChange(modelId) { return !disposed && postToHost(MESSAGE.MODEL_CHANGE, { modelId }) },
    dispose() {
      disposed = true
      cancel()
      for (const requestId of grants.keys()) finishGrant(requestId, errorWithCode('The client was disposed.', 'DISPOSED'))
      globalThis.window?.removeEventListener('message', receive)
    },
    get hostReady() { return hostReady },
    get activeRunId() { return active?.runId || null },
  }
}
