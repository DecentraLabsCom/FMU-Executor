import {
  createProtocolId, DIGITAL_TWIN_CAPABILITY, DIGITAL_TWIN_MESSAGE as MESSAGE,
  isHostMessageEvent, isValidOptions, isValidParameters,
  isValidSample, isRecord, postToHost, MAX_SAMPLES_PER_BATCH,
  isValidFmuStateCheckpointBundle, isValidBatchOptions, isValidBatchSimulationBody,
} from './index.js'
import { connectFmuSession } from './fmu-client.js'
import {
  createAdaptiveBatchController,
  getAdaptiveBatchSize,
  getRealtimePaceWaitMs,
  getRealtimePacedBatchSize,
  observeAdaptiveBatch,
} from './batch-controller.js'
import {
  getInputUpdateQuota,
  getInputUpdateIntervalMs,
  getInputUpdateRetryDelayMs,
} from './input-update-quota.js'

const errorWithCode = (message, code) => Object.assign(new Error(message), { code })
const MAX_RUN_INPUT_SCHEDULE_EVENTS = 200_000
const ADVANCE_INPUT_SCHEDULE_TOLERANCE = 1e-12
const VALID_EXECUTION_PACES = new Set(['fastest', 'realtime'])
const UTF8_ENCODER = new TextEncoder()
const VALID_ADVANCE_STOP_REASONS = new Set([
  'target_reached', 'max_samples', 'compute_budget', 'early_return', 'simulation_finished',
])

function setNativeControlProperty(element, property, value) {
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), property)?.set
  if (setter) setter.call(element, value)
  else element[property] = value
}

function applyModelValuesToNamedControls(modelValues) {
  const document = globalThis.document
  if (!isRecord(modelValues) || !document?.querySelectorAll) return
  const window = document.defaultView
  const controls = document.querySelectorAll('input[name], select[name], textarea[name]')
  for (const control of controls) {
    if (!Object.prototype.hasOwnProperty.call(modelValues, control.name)) continue
    const value = modelValues[control.name]
    const input = String(control.tagName).toLowerCase() === 'input' ? control : null
    if (input?.type === 'checkbox') {
      setNativeControlProperty(control, 'checked', Boolean(value))
    } else if (input?.type === 'radio') {
      setNativeControlProperty(control, 'checked', String(control.value) === String(value))
    } else if (!input || !['button', 'file', 'image', 'reset', 'submit'].includes(input.type)) {
      setNativeControlProperty(control, 'value', String(value))
    } else {
      continue
    }
    const Event = window?.Event || globalThis.Event
    if (Event) {
      control.dispatchEvent(new Event('input', { bubbles: true }))
      control.dispatchEvent(new Event('change', { bubbles: true }))
    }
  }
}

function isValidRunInputSchedule(value) {
  return Array.isArray(value) && value.length <= MAX_RUN_INPUT_SCHEDULE_EVENTS
    && value.every((event) => isRecord(event)
      && Object.keys(event).every((key) => ['time', 'values'].includes(key))
      && Number.isFinite(event.time)
      && isValidParameters(event.values))
}

function isValidAdvanceOptions(value) {
  return isRecord(value) && Object.entries(value).every(([key, option]) => {
    if (key === 'outputSampleEverySteps') return Number.isSafeInteger(option) && option >= 1 && option <= 100_000
    if (key === 'outputVariables') {
      return Array.isArray(option) && option.length > 0 && option.length <= 512
        && option.every((name) => typeof name === 'string' && name.length > 0 && name.length <= 256)
        && new Set(option).size === option.length
    }
    return false
  })
}

function createScheduledInputBatch(run, eventIndex, latestTime) {
  if (eventIndex >= run.schedule.length || run.schedule[eventIndex].time > latestTime + ADVANCE_INPUT_SCHEDULE_TOLERANCE) {
    return { events: [] }
  }
  const quota = run.scheduleQuota
  const events = []
  let encodedBytes = 2 // JSON array brackets
  let nextIndex = eventIndex
  while (nextIndex < run.schedule.length && events.length < quota.maxScheduledEventsPerAdvance) {
    const event = run.schedule[nextIndex]
    if (event.time > latestTime + ADVANCE_INPUT_SCHEDULE_TOLERANCE) break
    const eventBytes = UTF8_ENCODER.encode(JSON.stringify(event)).length
    const candidateBytes = encodedBytes + (events.length ? 1 : 0) + eventBytes
    if (candidateBytes > quota.maxScheduleBytesPerAdvance) {
      if (!events.length) {
        throw errorWithCode('A scheduled input event exceeds the FMU runtime advance limit.', 'INPUT_SCHEDULE_EVENT_TOO_LARGE')
      }
      break
    }
    events.push(event)
    encodedBytes = candidateBytes
    nextIndex += 1
  }
  return { events }
}

function hasValidAdvanceMetrics(response, samples) {
  return Number.isFinite(response?.computeMs) && response.computeMs >= 0
    && response.samplesReturned === samples.length
    && Number.isFinite(response.sampleBytes) && response.sampleBytes >= 0
    && Number.isSafeInteger(response.stepsExecuted) && response.stepsExecuted > 0
    && Number.isSafeInteger(response.inputEventsApplied) && response.inputEventsApplied >= 0
    && Number.isFinite(response.computeBudgetOverrunMs) && response.computeBudgetOverrunMs >= 0
    && VALID_ADVANCE_STOP_REASONS.has(response.stopReason)
}

const wait = (ms, signal) => new Promise((resolve, reject) => {
  const aborted = () => { clearTimeout(timer); reject(signal.reason) }
  const timer = setTimeout(() => { signal.removeEventListener('abort', aborted); resolve() }, ms)
  signal.addEventListener('abort', aborted, { once: true })
  if (signal.aborted) aborted()
})

/** Optional host UI integration composed with the independent direct FMU client. */
export function createDigitalTwinAppClient({
  getConnection, onHostReady, onSample, onSamples, onReset, onSimulationState,
  onFmiLsParameterPreset, onFmiLsExperiment,
  onFmuStateCheckpoint, onFmuStateLoaded,
  onBatchRendered,
  onResultsComplete, onPlaybackRate, onRunAccepted, onRunRejected, onRunCancelled, onError,
  executionPaceControl = false, defaultExecutionPace, onExecutionPace,
  defaultPlaybackRate, playbackControlPosition, runRequestTimeoutMs = 30_000,
} = {}) {
  if (typeof executionPaceControl !== 'boolean') throw errorWithCode('executionPaceControl must be a boolean.', 'INVALID_INPUT')
  const configuredExecutionPace = defaultExecutionPace ?? (executionPaceControl ? 'realtime' : 'fastest')
  if (!VALID_EXECUTION_PACES.has(configuredExecutionPace)) {
    throw errorWithCode('defaultExecutionPace must be fastest or realtime.', 'INVALID_INPUT')
  }
  let disposed = false
  let hostReady = typeof getConnection === 'function'
  let sceneReady = false
  let readySent = false
  let startupError
  let active = null
  let completedRun = null
  let completedRunTimer = null
  let selectedFmiLsExperiment = null
  let selectedFmuStateCheckpoint = null
  let resumeFromFmuStateCheckpoint = false
  let captureFmuStateOnCompletion = false
  let lastLoadedCheckpointId = null
  let hostExtensions = {}
  const grants = new Map()
  const batchRequests = new Map()
  const capabilities = [DIGITAL_TWIN_CAPABILITY.EXECUTION_CONTROL]
  capabilities.push(DIGITAL_TWIN_CAPABILITY.FMI_LS_EXPERIMENTS)
  capabilities.push(DIGITAL_TWIN_CAPABILITY.FMU_STATE_CHECKPOINTS)
  if (executionPaceControl) capabilities.push(DIGITAL_TWIN_CAPABILITY.EXECUTION_PACE)
  if (typeof onPlaybackRate === 'function') capabilities.push(DIGITAL_TWIN_CAPABILITY.PLAYBACK_RATE)
  if (typeof onFmiLsParameterPreset === 'function') capabilities.push(DIGITAL_TWIN_CAPABILITY.FMI_LS_PARAMETER_PRESETS)
  const report = (error) => { try { onError?.(error) } catch { /* The app owns its error presentation. */ } }

  function releaseCompletedRun() {
    if (completedRunTimer !== null) {
      clearTimeout(completedRunTimer)
      completedRunTimer = null
    }
    completedRun?.session?.close()
    completedRun = null
  }

  function sendReady() {
    if (!disposed && hostReady && sceneReady && !readySent && !startupError) {
      readySent = postToHost(MESSAGE.APP_READY, {
        capabilities,
        ...(defaultPlaybackRate === undefined ? {} : { defaultPlaybackRate }),
        ...(executionPaceControl ? { defaultExecutionPace: configuredExecutionPace } : {}),
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
      hostExtensions = message.extensions || {}
      if (!hostReady) {
        hostReady = true
        try { onHostReady?.(hostExtensions) } catch (error) { report(error) }
      }
      readySent = false
      if (startupError) postToHost(MESSAGE.APP_ERROR, { code: startupError })
      else sendReady()
    } else if (message.type === MESSAGE.CONNECTION_READY) {
      finishGrant(message.requestId, null, {
        ...(message.connection ? { connection: message.connection } : {}),
        ...(message.transport ? { transport: message.transport } : {}),
        inputSchedule: message.inputSchedule || [],
        ...(message.executionPace === undefined ? {} : { executionPace: message.executionPace }),
        ...(message.defaultStartTime === undefined ? {} : { defaultStartTime: message.defaultStartTime }),
        ...(message.defaultStepSize === undefined ? {} : { defaultStepSize: message.defaultStepSize }),
        ...(message.captureFmuStateAvailable === undefined ? {} : { captureFmuStateAvailable: message.captureFmuStateAvailable }),
      })
    } else if (message.type === MESSAGE.CONNECTION_ERROR) {
      finishGrant(message.requestId, errorWithCode('The provider connection could not be authorized.', message.code))
    } else if (message.type === MESSAGE.PLAYBACK_CONTROL) {
      try { onPlaybackRate?.(message.rate, message) } catch (error) { report(error) }
    } else if (message.type === MESSAGE.FMI_LS_PARAMETER_PRESET) {
      try { onFmiLsParameterPreset?.(message) } catch (error) { report(error) }
    } else if (message.type === MESSAGE.FMI_LS_EXPERIMENT) {
      selectedFmiLsExperiment = message.experiment ? {
        ...message.experiment,
        parameters: { ...message.experiment.parameters },
        options: { ...message.experiment.options },
      } : null
      try {
        onFmiLsExperiment?.(selectedFmiLsExperiment ? {
          ...selectedFmiLsExperiment,
          parameters: { ...selectedFmiLsExperiment.parameters },
          options: { ...selectedFmiLsExperiment.options },
        } : null)
      } catch (error) { report(error) }
    } else if (message.type === MESSAGE.FMU_STATE_CONFIGURATION) {
      selectedFmuStateCheckpoint = message.checkpoint
      resumeFromFmuStateCheckpoint = message.resume
      captureFmuStateOnCompletion = message.captureOnCompletion
      if (!message.resume) {
        lastLoadedCheckpointId = null
      } else if (message.checkpoint
        && (message.checkpoint.sample || message.checkpoint.modelValues)
        && message.checkpoint.checkpoint.checkpointId !== lastLoadedCheckpointId) {
        lastLoadedCheckpointId = message.checkpoint.checkpoint.checkpointId
        const modelValues = message.checkpoint.modelValues
          ? { ...message.checkpoint.modelValues }
          : undefined
        applyModelValuesToNamedControls(modelValues)
        if (message.checkpoint.sample) {
          const sample = {
            time: message.checkpoint.sample.time,
            outputs: { ...message.checkpoint.sample.outputs },
          }
          try {
            Promise.resolve(onFmuStateLoaded?.(sample, {
              checkpointId: lastLoadedCheckpointId,
              simTime: message.checkpoint.simTime,
              ...(modelValues ? { modelValues } : {}),
            })).catch(report)
          } catch (error) { report(error) }
        }
      }
    } else if (message.type === MESSAGE.BATCH_EXECUTION_EVENT) {
      const request = batchRequests.get(message.requestId)
      if (request && !request.closed) {
        request.queue.push({ event: message.event, eventId: message.eventId })
        request.wake?.()
        request.wake = null
      }
    } else if (message.type === MESSAGE.BATCH_EXECUTION_END) {
      const request = batchRequests.get(message.requestId)
      if (request && !request.closed) {
        request.closed = true
        if (message.code) request.error = errorWithCode('The batch simulation could not be completed.', message.code)
        request.wake?.()
        request.wake = null
      }
    } else if (message.type === MESSAGE.FMU_STATE_CAPTURE) {
      void captureState().catch(report)
    } else if (message.type === MESSAGE.EXECUTION_CANCEL && active?.runId === message.runId) cancel(message.runId)
  }
  globalThis.window?.addEventListener('message', receive)
  // Async apps (e.g. a React app loading its config) may mount after iframe load.
  postToHost(MESSAGE.APP_HELLO)
  if (getConnection) queueMicrotask(() => { if (!disposed) { try { onHostReady?.({}) } catch (error) { report(error) } } })

  function requestConnection(options, signal, { transport = 'websocket', executionPace } = {}) {
    if (getConnection) return Promise.resolve(getConnection({ options, signal })).then((grant) => grant.connection ? grant : { connection: grant, inputSchedule: [] })
    if (!hostReady || !readySent) return Promise.reject(errorWithCode('The host is not ready to authorize an FMU connection.', 'HOST_NOT_READY'))
    const requestId = createProtocolId('grant')
    return new Promise((resolve, reject) => {
      const abort = () => finishGrant(requestId, signal.reason)
      const timer = setTimeout(() => finishGrant(requestId, errorWithCode('The connection grant timed out.', 'REQUEST_TIMEOUT')), runRequestTimeoutMs)
      grants.set(requestId, { resolve, reject, timer, signal, abort })
      signal.addEventListener('abort', abort, { once: true })
      postToHost(MESSAGE.CONNECTION_REQUEST, {
        requestId,
        options,
        transport,
        ...(executionPace ? { executionPace } : {}),
      })
    })
  }

  function requestBatchSimulation(run, body, batchOptions) {
    if (!isValidBatchSimulationBody(body) || !isValidBatchOptions(batchOptions)) {
      throw errorWithCode('The batch simulation request is invalid.', 'INVALID_RUN_REQUEST')
    }
    const requestId = createProtocolId('batch')
    run.batchRequestId = requestId
    const pending = { queue: [], closed: false, error: null, wake: null }
    batchRequests.set(requestId, pending)
    const abort = () => {
      postToHost(MESSAGE.BATCH_EXECUTION_CANCEL, { requestId })
      pending.closed = true
      pending.error = run.controller.signal.reason || new DOMException('Cancelled', 'AbortError')
      pending.wake?.()
      pending.wake = null
    }
    run.controller.signal.addEventListener('abort', abort, { once: true })
    if (run.controller.signal.aborted) abort()
    if (!postToHost(MESSAGE.BATCH_EXECUTION_REQUEST, { requestId, runId: run.runId, body, batchOptions })) {
      pending.closed = true
      pending.error = errorWithCode('The host cannot execute batch simulations.', 'BATCH_EXECUTION_UNAVAILABLE')
    }
    return {
      async *[Symbol.asyncIterator]() {
        try {
          while (true) {
            if (pending.queue.length) {
              yield pending.queue.shift()
              continue
            }
            if (pending.closed) {
              if (pending.error) throw pending.error
              return
            }
            await new Promise((resolve) => { pending.wake = resolve })
          }
        } finally {
          if (!pending.closed) postToHost(MESSAGE.BATCH_EXECUTION_CANCEL, { requestId })
          run.controller.signal.removeEventListener('abort', abort)
          batchRequests.delete(requestId)
        }
      },
    }
  }

  function state(run, value) {
    if (disposed) return
    const options = run.runOptions || run.options
    try { onSimulationState?.(value, { runId: run.runId, state: value, options }) } catch (error) { report(error) }
    postToHost(MESSAGE.EXECUTION_STATE, { runId: run.runId, state: value, options })
  }
  async function deliver(run, samples) {
    run.controller.signal.throwIfAborted()
    if (!samples.length || samples.length > MAX_SAMPLES_PER_BATCH || !samples.every(isValidSample)) throw errorWithCode('Invalid FMU samples.', 'INVALID_FMU_RESPONSE')
    const selectedOutputs = run.advanceOptions.outputVariables
    if (selectedOutputs && samples.some((sample) => selectedOutputs.some((name) => !Object.prototype.hasOwnProperty.call(sample.outputs, name)))) {
      throw errorWithCode('The FMU response omitted a selected output.', 'INVALID_FMU_RESPONSE')
    }
    const context = { runId: run.runId, signal: run.controller.signal }
    const batch = { runId: run.runId, samples, firstSampleIndex: run.sampleCount, lastSampleIndex: run.sampleCount + samples.length - 1 }
    if (onSamples) await onSamples(samples, batch, context)
    else for (const sample of samples) { context.signal.throwIfAborted(); await onSample?.(sample, context) }
    run.controller.signal.throwIfAborted()
    run.sampleCount += samples.length
    const lastSample = samples.at(-1)
    run.lastSample = { time: lastSample.time, outputs: { ...lastSample.outputs } }
    return batch
  }
  async function operation(run, type, expected, payload) {
    const readsOutputs = ['sim.getOutputs', 'sim.advance'].includes(type)
    const inputValues = type === 'sim.setInputs' ? payload.values : type === 'sim.advance' ? payload.inputValues : null
    const writesInputs = isRecord(inputValues) && Object.keys(inputValues).length > 0
    const tracksAdaptiveResponse = run.batchPolicy === 'adaptive' && type === 'sim.advance'
    const operationStartedAt = performance.now()
    let rateLimitWaitMs = 0
    while (true) {
      run.controller.signal.throwIfAborted()
      try {
        if (writesInputs && run.lastInputUpdateAt !== null) {
          const waitMs = run.lastInputUpdateAt + getInputUpdateIntervalMs(run.session.capabilities) - performance.now()
          if (waitMs > 0) {
            const waitStartedAt = performance.now()
            await wait(Math.ceil(waitMs), run.controller.signal)
            rateLimitWaitMs += performance.now() - waitStartedAt
          }
        }
        if (readsOutputs && run.nextOutputAt > performance.now()) {
          const throttleWaitStartedAt = performance.now()
          await wait(Math.ceil(run.nextOutputAt - throttleWaitStartedAt), run.controller.signal)
          rateLimitWaitMs += performance.now() - throttleWaitStartedAt
        }
        const response = await run.session.request(type, expected, payload)
        if (writesInputs) {
          run.lastInputUpdateAt = performance.now()
          run.modelValues = { ...run.modelValues, ...inputValues }
        }
        if (readsOutputs) {
          if (tracksAdaptiveResponse) {
            run.lastOutputMetrics = {
              responseMs: performance.now() - operationStartedAt,
              rateLimitWaitMs,
            }
          }
          run.nextOutputAt = performance.now() + run.outputIntervalMs
        }
        return response
      }
      catch (error) {
        const inputRetryAfterMs = writesInputs
          ? getInputUpdateRetryDelayMs(error, run.session.capabilities)
          : null
        if (inputRetryAfterMs !== null) {
          const waitStartedAt = performance.now()
          await wait(inputRetryAfterMs, run.controller.signal)
          rateLimitWaitMs += performance.now() - waitStartedAt
          continue
        }
        if (error.code !== 'OUTPUT_READ_RATE_LIMITED' || !Number.isFinite(error.retryAfterMs) || error.retryAfterMs < 1 || error.retryAfterMs > 10_000) throw error
        const retryWaitStartedAt = performance.now()
        await wait(error.retryAfterMs, run.controller.signal)
        rateLimitWaitMs += performance.now() - retryWaitStartedAt
      }
    }
  }
  function notifyCheckpoint(bundle) {
    try { onFmuStateCheckpoint?.(bundle) } catch (error) { report(error) }
    postToHost(MESSAGE.FMU_STATE_SAVED, { bundle })
  }
  async function captureState(run = active || completedRun) {
    if (run?.batchCheckpointBundle) {
      notifyCheckpoint(run.batchCheckpointBundle)
      if (run === completedRun) releaseCompletedRun()
      return run.batchCheckpointBundle
    }
    if (!run?.session) throw errorWithCode('There is no active FMU session to capture.', 'RUN_UNAVAILABLE')
    try {
      const capturedBundle = await run.session.captureState()
      const bundleWithDisplay = {
        ...capturedBundle,
        ...(run.lastSample ? { sample: run.lastSample } : {}),
        ...(run.modelValues && Object.keys(run.modelValues).length ? { modelValues: run.modelValues } : {}),
      }
      const bundleWithValues = {
        ...capturedBundle,
        ...(run.modelValues && Object.keys(run.modelValues).length ? { modelValues: run.modelValues } : {}),
      }
      const bundleWithSample = run.lastSample ? { ...capturedBundle, sample: run.lastSample } : capturedBundle
      const bundle = isValidFmuStateCheckpointBundle(bundleWithDisplay)
        ? bundleWithDisplay
        : isValidFmuStateCheckpointBundle(bundleWithValues)
          ? bundleWithValues
          : isValidFmuStateCheckpointBundle(bundleWithSample) ? bundleWithSample : capturedBundle
      notifyCheckpoint(bundle)
      if (run === completedRun) releaseCompletedRun()
      return bundle
    } catch (error) {
      postToHost(MESSAGE.FMU_STATE_ERROR, { code: 'FMU_STATE_CAPTURE_FAILED' })
      throw error
    }
  }
  async function finish(run, outcome, error) {
    if (run.finished) return
    run.finished = true
    let checkpoint
    if (outcome === 'completed' && run.captureFmuStateOnComplete) {
      try { checkpoint = await captureState(run) } catch (captureError) {
        outcome = 'error'
        error = captureError
      }
    }
    const retainCompletedRun = outcome === 'completed' && !disposed
      && (run.batchCheckpointBundle || (run.session?.capabilities?.fmuState?.getAndSet === true
        && run.session.capabilities?.fmuState?.serialize === true))
    if (retainCompletedRun) {
      releaseCompletedRun()
      completedRun = run
      completedRunTimer = setTimeout(() => {
        if (completedRun === run) releaseCompletedRun()
      }, 5 * 60 * 1000)
    } else {
      run.session?.close()
    }
    if (disposed) { if (active === run) active = null; return }
    if (error && outcome === 'error') report(error)
    state(run, outcome)
    if (outcome === 'cancelled') { try { onRunCancelled?.(run.runId) } catch (callbackError) { report(callbackError) } }
    try {
      await onResultsComplete?.({ runId: run.runId, state: outcome, sampleCount: run.sampleCount, ...(checkpoint ? { checkpoint } : {}) }, { runId: run.runId, signal: run.controller.signal })
    } catch (callbackError) { report(callbackError) }
    finally { if (active === run) active = null }
  }
  async function updateController(run, sample, { initial = false } = {}) {
    if (!run.inputController) return
    const sampleTime = Number(sample?.time)
    const tolerance = Math.max(1e-9, Math.abs(run.options.stepSize) * 1e-7)
    if (!initial && run.inputControllerInterval) {
      const intervalReached = sampleTime >= run.nextControllerTime - tolerance
      const scheduledTime = run.inputControllerTimes[run.inputControllerTimeIndex]
      const scheduledTimeReached = Number.isFinite(scheduledTime) && sampleTime >= scheduledTime - tolerance
      if (!intervalReached && !scheduledTimeReached) return
      if (intervalReached) {
        while (run.nextControllerTime <= sampleTime + tolerance) run.nextControllerTime += run.inputControllerInterval
      }
      while (run.inputControllerTimes[run.inputControllerTimeIndex] <= sampleTime + tolerance) {
        run.inputControllerTimeIndex += 1
      }
    } else if (!initial && run.inputControllerTimes.length) {
      const scheduledTime = run.inputControllerTimes[run.inputControllerTimeIndex]
      if (!Number.isFinite(scheduledTime) || sampleTime < scheduledTime - tolerance) return
      while (run.inputControllerTimes[run.inputControllerTimeIndex] <= sampleTime + tolerance) {
        run.inputControllerTimeIndex += 1
      }
    }
    const values = await run.inputController(sample, { runId: run.runId, signal: run.controller.signal })
    run.controller.signal.throwIfAborted()
    if (values && Object.keys(values).length) {
      if (!isValidParameters(values)) throw errorWithCode('Invalid controller inputs.', 'INVALID_INPUT')
      run.pendingInputValues = { ...(run.pendingInputValues || {}), ...values }
    }
  }

  async function pumpBatchHttp(run, body, batchOptions) {
    try {
      let completedEvent = null
      for await (const { event, eventId } of requestBatchSimulation(run, body, batchOptions)) {
        run.controller.signal.throwIfAborted()
        try {
          if (event.kind === 'samples') {
            await deliver(run, event.samples)
          } else if (event.kind === 'completed') {
            completedEvent = event
            if (event.checkpoint) {
              const checkpointTime = event.checkpointTime ?? event.time
              const modelValues = { ...(run.modelValues || {}) }
              for (const scheduled of run.schedule || []) {
                if (scheduled.time <= checkpointTime + 1e-12) Object.assign(modelValues, scheduled.values)
              }
              const sample = event.checkpointSample
                || (run.lastSample && Math.abs(run.lastSample.time - checkpointTime) <= 1e-9
                  ? run.lastSample
                  : undefined)
              const checkpointBundle = {
                version: 1,
                simTime: checkpointTime,
                checkpoint: event.checkpoint,
                ...(sample ? { sample } : {}),
                ...(Object.keys(modelValues).length ? { modelValues } : {}),
              }
              if (!isValidFmuStateCheckpointBundle(checkpointBundle)) {
                throw errorWithCode('The FMU state checkpoint response is invalid.', 'INVALID_FMU_STATE')
              }
              run.batchCheckpointBundle = checkpointBundle
            }
          } else if (event.kind === 'error') {
            throw errorWithCode(event.message || 'The batch simulation could not be completed.', event.code)
          }
        } finally {
          postToHost(MESSAGE.BATCH_EXECUTION_ACK, { requestId: run.batchRequestId, eventId })
        }
      }
      if (!completedEvent) throw errorWithCode('The batch simulation ended without a completion result.', 'INVALID_FMU_RESPONSE')
      if (run.batchCaptureExpected && !run.batchCheckpointBundle) {
        throw errorWithCode('The provider did not return the requested FMU state checkpoint.', 'FMU_STATE_CAPTURE_FAILED')
      }
      run.finalTime = completedEvent.time
      await finish(run, 'completed')
    } catch (error) {
      await finish(run, run.controller.signal.aborted ? 'cancelled' : 'error', error)
    }
  }

  async function pump(run) {
    try {
      let time = run.resumeTime ?? run.options.startTime
      let eventIndex = 0
      const stopTime = run.options.stopTime
      const hasStopTime = Number.isFinite(stopTime)
      while (!hasStopTime || time < stopTime - 1e-12) {
        run.controller.signal.throwIfAborted()
        const scheduleTolerance = Math.max(1e-9, Math.abs(run.options.stepSize) * 1e-7)
        const hasDueSchedule = eventIndex < run.schedule.length
          && run.schedule[eventIndex].time <= time + scheduleTolerance
        if (hasDueSchedule && run.pendingInputValues && Object.keys(run.pendingInputValues).length) {
          await operation(run, 'sim.setInputs', 'sim.inputs.updated', { values: run.pendingInputValues })
          run.pendingInputValues = null
        }
        while (eventIndex < run.schedule.length && run.schedule[eventIndex].time <= time + scheduleTolerance) {
          await operation(run, 'sim.setInputs', 'sim.inputs.updated', { values: run.schedule[eventIndex++].values })
        }
        if (run.executionPace === 'realtime') {
          let waitMs = getRealtimePaceWaitMs({
            currentTime: time,
            stopTime,
            stepSize: run.options.stepSize,
            sampleEverySteps: run.advanceOptions.outputSampleEverySteps ?? 1,
            paceClock: run.paceClock,
            now: performance.now(),
          })
          while (waitMs > 0) {
            await wait(Math.min(waitMs, 100), run.controller.signal)
            waitMs = getRealtimePaceWaitMs({
              currentTime: time,
              stopTime,
              stepSize: run.options.stepSize,
              sampleEverySteps: run.advanceOptions.outputSampleEverySteps ?? 1,
              paceClock: run.paceClock,
              now: performance.now(),
            })
          }
        }
        const nextControllerTime = run.inputController
          ? Math.min(
            run.nextControllerTime ?? Infinity,
            run.inputControllerTimes[run.inputControllerTimeIndex] ?? Infinity,
          )
          : Infinity
        const controllerTargetTime = hasStopTime
          ? Math.min(stopTime, nextControllerTime)
          : nextControllerTime
        const outputSampleEverySteps = run.advanceOptions.outputSampleEverySteps ?? 1
        const controllerSampleLimit = run.inputController
          ? run.inputControllerInterval || run.inputControllerTimes.length
            ? Math.max(1, Math.ceil((controllerTargetTime - time) / (run.options.stepSize * outputSampleEverySteps)) + 1)
            : 1
          : run.maxBatchSamples
        const maxSamples = run.batchPolicy === 'adaptive'
          ? Math.min(getAdaptiveBatchSize(run.batchController), controllerSampleLimit)
          : run.inputController
            ? Math.min(run.maxBatchSamples, controllerSampleLimit)
            : run.maxBatchSamples
        const pacedMaxSamples = run.executionPace === 'realtime'
          ? getRealtimePacedBatchSize({
            currentTime: time,
            stopTime,
            stepSize: run.options.stepSize,
            sampleEverySteps: outputSampleEverySteps,
            maxSamples,
            paceClock: run.paceClock,
            now: performance.now(),
          })
          : maxSamples
        const maxSampleTargetTime = hasStopTime
          ? Math.min(stopTime, time + pacedMaxSamples * outputSampleEverySteps * run.options.stepSize)
          : time + pacedMaxSamples * outputSampleEverySteps * run.options.stepSize
        // Scheduled advances are rejected before execution when their time
        // span would produce more samples than maxSamples. Keep the schedule
        // horizon within the current batch budget, including the first
        // adaptive batch while its estimate is still one sample.
        let scheduleBatch = run.schedule.length
          ? createScheduledInputBatch(run, eventIndex, Math.min(controllerTargetTime, maxSampleTargetTime))
          : { events: [] }
        const scheduledTargetTime = scheduleBatch.events.length
          ? scheduleBatch.events.at(-1).time
          : Infinity
        let targetTime = Math.min(controllerTargetTime, maxSampleTargetTime, scheduledTargetTime)
        if (!Number.isFinite(targetTime)) targetTime = maxSampleTargetTime
        if (run.pendingInputValues && Object.keys(run.pendingInputValues).length) {
          // Current-time scheduled changes are sent before the next advance;
          // flush a controller update first so schedule order remains intact.
          const firstScheduledEvent = scheduleBatch.events[0]
          if (firstScheduledEvent && firstScheduledEvent.time <= time + scheduleTolerance) {
            await operation(run, 'sim.setInputs', 'sim.inputs.updated', { values: run.pendingInputValues })
            run.pendingInputValues = null
          }
        }
        if (scheduleBatch.events.length) {
          const pacedScheduleBatch = createScheduledInputBatch(run, eventIndex, targetTime)
          scheduleBatch = pacedScheduleBatch
        }
        if (targetTime <= time + scheduleTolerance) {
          targetTime = hasStopTime
            ? Math.min(stopTime, time + run.options.stepSize)
            : time + run.options.stepSize
        }
        let pendingInputValues = run.pendingInputValues
        if (pendingInputValues && Object.keys(pendingInputValues).length
          && run.session.capabilities.advanceInputValues !== true) {
          await operation(run, 'sim.setInputs', 'sim.inputs.updated', { values: pendingInputValues })
          if (run.pendingInputValues === pendingInputValues) run.pendingInputValues = null
          pendingInputValues = null
        }
        const result = await operation(run, 'sim.advance', 'sim.samples', {
          targetTime,
          maxSamples: pacedMaxSamples,
          stepSize: run.options.stepSize,
          ...(scheduleBatch.events.length ? { inputSchedule: scheduleBatch.events } : {}),
          ...(pendingInputValues && Object.keys(pendingInputValues).length ? { inputValues: pendingInputValues } : {}),
          ...run.advanceOptions,
          ...(run.batchPolicy === 'adaptive' ? { maxComputeMs: run.batchController.maxComputeMs } : {}),
        })
        if (pendingInputValues && run.pendingInputValues === pendingInputValues) run.pendingInputValues = null
        const batchReceivedAt = run.batchPolicy === 'adaptive' ? performance.now() : null
        const tolerance = Math.max(1e-9, Math.abs(run.options.stepSize) * 1e-7)
        if (!Array.isArray(result.samples) || !result.samples.length || !Number.isFinite(result.simTime)
          || result.simTime < time - tolerance || result.simTime > targetTime + tolerance) throw errorWithCode('The FMU did not advance as requested.', 'INVALID_FMU_RESPONSE')
        if (run.batchPolicy === 'adaptive'
          && (!hasValidAdvanceMetrics(result, result.samples)
            || result.maxComputeMs !== run.batchController.maxComputeMs
            || result.samples.some((sample) => !Number.isSafeInteger(sample?.sequence) || sample.sequence < 1))) {
          throw errorWithCode('The FMU returned invalid compute-budget or event-sequence metadata.', 'INVALID_FMU_RESPONSE')
        }
        let previous = time
        let previousSequence = run.lastSampleSequence
        let sequenceMode = Number.isSafeInteger(previousSequence) ? true : null
        for (const sample of result.samples) {
          const hasSequence = Number.isSafeInteger(sample.sequence) && sample.sequence > 0
          const sameTime = Math.abs(sample.time - previous) <= tolerance
          if (!Number.isFinite(sample.time) || sample.time < previous - tolerance || sample.time > result.simTime + tolerance) throw errorWithCode('The FMU returned an unordered timeline.', 'INVALID_FMU_RESPONSE')
          if ((sameTime && !hasSequence)
            || (hasSequence && sequenceMode === false)
            || (!hasSequence && sequenceMode === true)
            || (hasSequence && Number.isSafeInteger(previousSequence) && sample.sequence <= previousSequence)) {
            throw errorWithCode('The FMU returned unordered or unsequenced same-time event samples.', 'INVALID_FMU_RESPONSE')
          }
          if (hasSequence) {
            sequenceMode = true
            previousSequence = sample.sequence
          } else {
            sequenceMode = false
          }
          previous = sample.time
        }
        if (Math.abs(previous - result.simTime) > tolerance) throw errorWithCode('The FMU returned an incomplete timeline.', 'INVALID_FMU_RESPONSE')
        if (Math.abs(result.simTime - time) <= tolerance
          && (!Number.isSafeInteger(previousSequence) || previousSequence <= (run.lastSampleSequence ?? 0))) {
          throw errorWithCode('The FMU returned no new step or event.', 'INVALID_FMU_RESPONSE')
        }
        if (scheduleBatch.events.length) {
          const appliedEvents = result.inputEventsApplied
          if (!Number.isSafeInteger(appliedEvents) || appliedEvents < 0 || appliedEvents > scheduleBatch.events.length) {
            throw errorWithCode('The FMU returned an invalid scheduled input count.', 'INVALID_FMU_RESPONSE')
          }
          const firstUnappliedEvent = run.schedule[eventIndex + appliedEvents]
          if (firstUnappliedEvent && firstUnappliedEvent.time < result.simTime - tolerance) {
            throw errorWithCode('The FMU advanced past a scheduled input that it did not apply.', 'INVALID_FMU_RESPONSE')
          }
          for (const event of scheduleBatch.events.slice(0, appliedEvents)) {
            run.modelValues = { ...run.modelValues, ...event.values }
          }
          eventIndex += appliedEvents
        }
        run.lastSampleSequence = Number.isSafeInteger(previousSequence) ? previousSequence : null
        const batch = await deliver(run, result.samples)
        time = result.simTime
        if (run.batchPolicy === 'adaptive') {
          let consumerDelayMs = null
          if (typeof onBatchRendered === 'function') {
            await onBatchRendered(result.samples, batch, { runId: run.runId, signal: run.controller.signal })
            consumerDelayMs = performance.now() - batchReceivedAt
          }
          run.batchController = observeAdaptiveBatch(run.batchController, {
            responseMs: run.lastOutputMetrics?.responseMs,
            computeMs: result.computeMs,
            sampleCount: result.samplesReturned ?? result.samples.length,
            payloadBytes: result.sampleBytes,
            consumerDelayMs,
            rateLimitWaitMs: run.lastOutputMetrics?.rateLimitWaitMs,
            stopReason: result.stopReason,
          })
        }
        if (result.finished) {
          if (eventIndex < run.schedule.length) throw errorWithCode('The FMU finished before applying the complete input schedule.', 'FMU_FINISHED_BEFORE_INPUT_SCHEDULE')
          break
        }
        await updateController(run, result.samples.at(-1))
      }
      if (eventIndex < run.schedule.length) throw errorWithCode('The FMU finished before applying the complete input schedule.', 'FMU_FINISHED_BEFORE_INPUT_SCHEDULE')
      await finish(run, 'completed')
    } catch (error) { await finish(run, run.controller.signal.aborted ? 'cancelled' : 'error', error) }
  }

  async function startBatchHttpRun(run, grant, {
    parameters, options, inputSchedule, composeInputSchedule, shouldCaptureFmuState,
  }) {
    if (grant.executionPace !== 'fastest') {
      throw errorWithCode('The host authorized an incompatible execution pace for HTTP batch mode.', 'INVALID_HOST_MESSAGE')
    }
    const startTime = options.startTime ?? grant.defaultStartTime ?? 0
    const stopTime = options.stopTime
    const stepSize = options.stepSize ?? grant.defaultStepSize ?? 0.001
    const resolvedOptions = {
      startTime,
      ...(stopTime === undefined ? {} : { stopTime }),
      stepSize,
      ...(options.tolerance === undefined ? {} : { tolerance: options.tolerance }),
    }
    if (!isValidOptions(resolvedOptions) || !Number.isFinite(stopTime)
      || stopTime <= startTime || stepSize <= 0) {
      throw errorWithCode('HTTP batch execution requires a finite, valid simulation interval.', 'INVALID_RUN_REQUEST')
    }
    const resumeTime = run.checkpointBundle?.simTime ?? startTime
    if (run.checkpointBundle && resumeTime < startTime) {
      throw errorWithCode('The saved FMU state is earlier than the configured simulation start time.', 'INVALID_RUN_REQUEST')
    }
    if (resumeTime >= stopTime) {
      throw errorWithCode('Stop time must be later than the saved FMU state. Stop time is absolute.', 'INVALID_RUN_REQUEST')
    }
    if (shouldCaptureFmuState && grant.captureFmuStateAvailable !== true) {
      throw errorWithCode('The provider FMU runtime does not support serialized FMU states.', 'UNSUPPORTED_FMU_STATE')
    }

    const runParameters = { ...parameters }
    run.modelValues = { ...runParameters, ...(run.checkpointBundle?.modelValues || {}) }
    run.options = resolvedOptions
    run.resumeTime = resumeTime
    run.runOptions = { ...resolvedOptions, startTime: resumeTime }
    run.executionPace = 'fastest'
    run.batchPolicy = 'fixed'
    run.schedule = []
    run.batchCaptureExpected = grant.captureFmuStateAvailable === true

    const candidateSchedule = composeInputSchedule
      ? await composeInputSchedule([...(grant.inputSchedule || [])])
      : inputSchedule ?? grant.inputSchedule ?? []
    if (!isValidRunInputSchedule(candidateSchedule)) {
      throw errorWithCode('Invalid input schedule.', 'INVALID_INPUT_SCHEDULE')
    }
    const schedule = run.checkpointBundle
      ? candidateSchedule.filter((event) => event.time >= resumeTime - 1e-12)
      : candidateSchedule
    if (!isValidRunInputSchedule(schedule)
      || schedule.some((event) => event.time < resumeTime || event.time >= stopTime)) {
      throw errorWithCode('Invalid input schedule.', 'INVALID_INPUT_SCHEDULE')
    }
    run.schedule = [...schedule].sort((a, b) => a.time - b.time)

    const body = {
      parameters: run.checkpointBundle ? {} : runParameters,
      options: { ...resolvedOptions, startTime: resumeTime },
      ...(run.schedule.length ? { inputSchedule: run.schedule } : {}),
      ...(run.checkpointBundle ? { checkpoint: run.checkpointBundle.checkpoint } : {}),
      ...(grant.captureFmuStateAvailable || shouldCaptureFmuState ? { captureFmuState: true } : {}),
    }
    const batchOptions = {
      maxSamples: run.maxBatchSamples,
      ...(run.advanceOptions.outputVariables ? { outputVariables: run.advanceOptions.outputVariables } : {}),
      ...(run.advanceOptions.outputSampleEverySteps === undefined
        ? {} : { outputSampleEverySteps: run.advanceOptions.outputSampleEverySteps }),
    }
    if (!isValidBatchSimulationBody(body) || !isValidBatchOptions(batchOptions)) {
      throw errorWithCode('The HTTP batch simulation request is invalid.', 'INVALID_RUN_REQUEST')
    }

    onReset?.(run.runId)
    state(run, 'running')
    onRunAccepted?.({ runId: run.runId })
    try { onExecutionPace?.('fastest') } catch (error) { report(error) }
    void pumpBatchHttp(run, body, batchOptions).catch(report)
    return { runId: run.runId }
  }

  async function run({
    parameters = {}, options = {}, inputSchedule, composeInputSchedule, inputController, batchPolicy,
    maxBatchSamples = MAX_SAMPLES_PER_BATCH, adaptiveBatchOptions = {}, advanceOptions = {},
    executionPace: requestedExecutionPace,
    validateModel, parametersFromModel,
    inputControllerInterval, inputControllerTimes = [],
    checkpoint: requestedCheckpoint, captureFmuStateOnComplete: requestedCaptureOnComplete,
  } = {}) {
    if (disposed || active) throw errorWithCode('The app cannot start another FMU run.', 'RUN_UNAVAILABLE')
    const checkpointBundle = requestedCheckpoint
      || (resumeFromFmuStateCheckpoint ? selectedFmuStateCheckpoint : null)
    if (checkpointBundle && !isValidFmuStateCheckpointBundle(checkpointBundle)) {
      throw errorWithCode('The FMU state checkpoint is invalid.', 'INVALID_FMU_STATE_CHECKPOINT')
    }
    const shouldCaptureFmuState = requestedCaptureOnComplete ?? captureFmuStateOnCompletion
    if (typeof shouldCaptureFmuState !== 'boolean') {
      throw errorWithCode('captureFmuStateOnComplete must be a boolean.', 'INVALID_INPUT')
    }
    parameters = selectedFmiLsExperiment
      ? { ...selectedFmiLsExperiment.parameters }
      : { ...parameters }
    options = { ...options, ...(selectedFmiLsExperiment?.options || {}) }
    if (!isValidParameters(parameters) || !isValidOptions(options)) throw errorWithCode('Invalid FMU run parameters.', 'INVALID_RUN_REQUEST')
    if (inputController !== undefined && typeof inputController !== 'function') throw errorWithCode('Invalid input controller.', 'INVALID_INPUT')
    if (inputControllerInterval !== undefined
      && (!Number.isFinite(inputControllerInterval) || inputControllerInterval <= 0)) {
      throw errorWithCode('inputControllerInterval must be a positive number of seconds.', 'INVALID_INPUT')
    }
    if (inputControllerTimes !== undefined
      && (!Array.isArray(inputControllerTimes) || inputControllerTimes.length > MAX_RUN_INPUT_SCHEDULE_EVENTS
        || inputControllerTimes.some((time) => !Number.isFinite(time)))) {
      throw errorWithCode('inputControllerTimes must be a bounded list of finite simulation times.', 'INVALID_INPUT')
    }
    if (requestedExecutionPace !== undefined && !VALID_EXECUTION_PACES.has(requestedExecutionPace)) {
      throw errorWithCode('executionPace must be fastest or realtime.', 'INVALID_INPUT')
    }
    const effectiveInputControllerTimes = checkpointBundle
      ? inputControllerTimes.filter((time) => time >= checkpointBundle.simTime - 1e-12)
      : inputControllerTimes
    if ((inputControllerInterval !== undefined || inputControllerTimes.length) && !inputController) {
      throw errorWithCode('Input controller timing requires an input controller.', 'INVALID_INPUT')
    }
    if ((validateModel !== undefined && typeof validateModel !== 'function')
      || (parametersFromModel !== undefined && typeof parametersFromModel !== 'function')) {
      throw errorWithCode('Invalid model validation options.', 'INVALID_INPUT')
    }
    if (composeInputSchedule !== undefined && typeof composeInputSchedule !== 'function') {
      throw errorWithCode('composeInputSchedule must be a function.', 'INVALID_INPUT')
    }
    if (inputSchedule !== undefined && composeInputSchedule !== undefined) {
      throw errorWithCode('Use either inputSchedule or composeInputSchedule.', 'INVALID_INPUT')
    }
    if (batchPolicy !== undefined && !['fixed', 'adaptive'].includes(batchPolicy)) throw errorWithCode('Invalid batch policy.', 'INVALID_INPUT')
    if (!isRecord(adaptiveBatchOptions)) throw errorWithCode('Invalid adaptive batch options.', 'INVALID_INPUT')
    if (!isValidAdvanceOptions(advanceOptions)) throw errorWithCode('Invalid FMU advance options.', 'INVALID_INPUT')
    if (!Number.isInteger(maxBatchSamples) || maxBatchSamples < 1 || maxBatchSamples > MAX_SAMPLES_PER_BATCH) throw errorWithCode(`maxBatchSamples must be an integer from 1 to ${MAX_SAMPLES_PER_BATCH}.`, 'INVALID_INPUT')
    releaseCompletedRun()
    const current = {
      runId: createProtocolId('run'), controller: new AbortController(), sampleCount: 0,
      options, inputController, inputControllerInterval, inputControllerTimes: [...effectiveInputControllerTimes].sort((a, b) => a - b),
      inputControllerTimeIndex: 0,
      advanceOptions: {
        ...advanceOptions,
        ...(advanceOptions.outputVariables ? { outputVariables: [...advanceOptions.outputVariables] } : {}),
      },
      validateModel, parametersFromModel,
      checkpointBundle, captureFmuStateOnComplete: shouldCaptureFmuState,
      batchPolicy, maxBatchSamples, adaptiveBatchOptions,
      lastSampleSequence: null, lastInputUpdateAt: null,
      pendingInputValues: null,
    }
    active = current
    try {
      const preferredPace = requestedExecutionPace ?? configuredExecutionPace
      const canUseBatchHttp = !getConnection
        && hostExtensions.batchPostStream === true
        && Number.isFinite(options.stopTime)
        && !inputController
        && !inputControllerTimes.length
        && inputControllerInterval === undefined
        && !validateModel
        && !parametersFromModel
        && batchPolicy !== 'adaptive'
        && preferredPace === 'fastest'
      const grant = await requestConnection(options, current.controller.signal, {
        transport: canUseBatchHttp ? 'batch-http' : 'websocket',
        executionPace: preferredPace,
      })
      current.controller.signal.throwIfAborted()
      if (grant.executionPace !== undefined && !VALID_EXECUTION_PACES.has(grant.executionPace)) {
        throw errorWithCode('The host returned an invalid execution pace.', 'INVALID_HOST_MESSAGE')
      }
      current.executionPace = grant.executionPace ?? requestedExecutionPace ?? configuredExecutionPace
      if (grant.transport === 'batch-http') {
        current.maxBatchSamples = maxBatchSamples
        await startBatchHttpRun(current, grant, {
          parameters,
          options,
          inputSchedule,
          composeInputSchedule,
          shouldCaptureFmuState,
        })
        return { runId: current.runId }
      }
      current.batchPolicy = batchPolicy ?? (current.executionPace === 'realtime' ? 'adaptive' : 'fixed')
      current.maxBatchSamples = maxBatchSamples
      const perSampleInputController = Boolean(inputController)
        && inputControllerInterval === undefined
        && effectiveInputControllerTimes.length === 0
      if (current.batchPolicy === 'adaptive' && perSampleInputController && batchPolicy === undefined) {
        // Closed-loop controllers that need every sample retain one-step
        // feedback; other real-time runs use the shared adaptive batching.
        current.batchPolicy = 'fixed'
        current.maxBatchSamples = 1
      }
      if (current.batchPolicy === 'adaptive' && perSampleInputController) {
        throw errorWithCode('Adaptive batching cannot be combined with a per-sample input controller.', 'INVALID_INPUT')
      }
      try { onExecutionPace?.(current.executionPace) } catch (error) { report(error) }
      current.session = await connectFmuSession(grant.connection, { signal: current.controller.signal })
      if ((checkpointBundle || shouldCaptureFmuState)
        && (current.session.capabilities.fmuState?.getAndSet !== true
          || current.session.capabilities.fmuState?.serialize !== true)) {
        throw errorWithCode('The provider FMU runtime does not support serialized FMU states.', 'UNSUPPORTED_FMU_STATE')
      }
      if (current.session.capabilities.advanceSamples !== true) throw errorWithCode('The provider FMU runtime needs batched WebSocket support.', 'UNSUPPORTED_FMU_RUNTIME')
      if (current.batchPolicy === 'adaptive' && current.session.capabilities.advanceComputeBudget !== true) {
        throw errorWithCode('The provider FMU runtime needs compute-bounded advance support for adaptive batching.', 'UNSUPPORTED_ADAPTIVE_FMU_RUNTIME')
      }
      if (current.batchPolicy === 'adaptive' && current.session.capabilities.advanceSampleSequences !== true) {
        throw errorWithCode('The provider FMU runtime needs sequenced advance samples for adaptive batching.', 'UNSUPPORTED_ADAPTIVE_FMU_RUNTIME')
      }
      const outputHz = current.session.capabilities.maxOutputHz
      current.outputIntervalMs = Number.isFinite(outputHz) && outputHz >= 1 && outputHz <= 1000 ? 1000 / outputHz : 0
      const model = await current.session.request('model.describe', 'model.description', { includeVariables: true })
      if (validateModel) {
        const validModel = await validateModel(model)
        if (validModel === false) throw errorWithCode('The connected FMU does not match this application.', 'FMU_MODEL_MISMATCH')
      }
      const modelParameters = parametersFromModel && !checkpointBundle ? await parametersFromModel(model) : {}
      if (!isValidParameters(modelParameters)) throw errorWithCode('The FMU model produced invalid initialization parameters.', 'INVALID_INPUT')
      const runParameters = { ...modelParameters, ...parameters }
      if (!isValidParameters(runParameters)) throw errorWithCode('The FMU initialization parameters exceed the supported limits.', 'INVALID_INPUT')
      const checkpointModelValues = {
        ...runParameters,
        ...(checkpointBundle?.modelValues || {}),
      }
      current.modelValues = isValidParameters(checkpointModelValues)
        ? checkpointModelValues
        : { ...(checkpointBundle?.modelValues || {}) }
      current.options = {
        startTime: options.startTime ?? model.defaultStartTime ?? 0,
        ...(options.stopTime !== undefined ? { stopTime: options.stopTime } : {}),
        stepSize: options.stepSize ?? model.defaultStepSize ?? 0.001,
        ...(options.tolerance === undefined ? {} : { tolerance: options.tolerance }),
      }
      const { startTime, stopTime, stepSize } = current.options
      if (!isValidOptions(current.options) || (stopTime !== undefined && stopTime <= startTime) || stepSize <= 0) throw errorWithCode('Invalid simulation interval.', 'INVALID_RUN_REQUEST')
      const resumeTime = checkpointBundle?.simTime ?? startTime
      if (checkpointBundle && resumeTime < startTime) {
        throw errorWithCode('The saved FMU state is earlier than the configured simulation start time.', 'INVALID_RUN_REQUEST')
      }
      if (stopTime !== undefined && resumeTime >= stopTime) {
        throw errorWithCode('Stop time must be later than the saved FMU state. Stop time is absolute.', 'INVALID_RUN_REQUEST')
      }
      current.resumeTime = resumeTime
      current.runOptions = { ...current.options, startTime: resumeTime }
      if (current.inputControllerTimes.some((time) => time < resumeTime || (stopTime !== undefined && time > stopTime))) {
        throw errorWithCode('An input controller time is outside the simulation interval.', 'INVALID_INPUT')
      }
      current.nextControllerTime = inputControllerInterval === undefined ? Infinity : resumeTime + inputControllerInterval
      if (current.batchPolicy === 'adaptive') {
        current.batchController = createAdaptiveBatchController({
          startTime: resumeTime, stopTime, stepSize, maxSamples: maxBatchSamples, ...adaptiveBatchOptions,
        })
      }
      // Keep provider-authorized FMI-LS experiments and Schedule changes when
      // an app also needs to add its own model-specific timing events.
      const providerSchedule = Array.isArray(grant.inputSchedule) ? grant.inputSchedule : []
      const candidateSchedule = composeInputSchedule
        ? await composeInputSchedule([...providerSchedule])
        : inputSchedule ?? providerSchedule
      if (!isValidRunInputSchedule(candidateSchedule)) throw errorWithCode('Invalid input schedule.', 'INVALID_INPUT_SCHEDULE')
      const schedule = checkpointBundle && Array.isArray(candidateSchedule)
        ? candidateSchedule.filter((event) => event.time >= checkpointBundle.simTime - 1e-12)
        : candidateSchedule
      if (!isValidRunInputSchedule(schedule) || schedule.some((event) => event.time < startTime || (stopTime !== undefined && event.time > stopTime))) throw errorWithCode('Invalid input schedule.', 'INVALID_INPUT_SCHEDULE')
      if (schedule.length && current.session.capabilities.advanceInputValues !== true) {
        throw errorWithCode('The provider FMU runtime cannot apply scheduled inputs during advance.', 'UNSUPPORTED_SCHEDULED_INPUTS')
      }
      current.scheduleQuota = schedule.length ? getInputUpdateQuota(current.session.capabilities) : null
      current.schedule = [...schedule].sort((a, b) => a.time - b.time)
      await current.session.request('sim.initialize', 'sim.state', {
        parameters: checkpointBundle ? {} : runParameters,
        options: { ...current.options, timeMode: 'simtime' },
        ...(checkpointBundle ? { checkpoint: checkpointBundle.checkpoint } : {}),
      })
      onReset?.(current.runId)
      state(current, 'running')
      const initial = await operation(current, 'sim.getOutputs', 'sim.outputs', {
        ...(current.advanceOptions.outputVariables ? { variables: current.advanceOptions.outputVariables } : {}),
      })
      if (current.advanceOptions.outputVariables) {
        const rounding = Number.EPSILON * Math.max(1, Math.abs(resumeTime)) * 8
        const tolerance = Math.min(Math.abs(stepSize) / 4, Math.max(1e-9, Math.abs(stepSize) * 1e-7, rounding))
        if (!Number.isFinite(initial?.simTime) || Math.abs(initial.simTime - resumeTime) > tolerance) {
          throw errorWithCode('The FMU returned an invalid initial sample time.', 'INVALID_FMU_RESPONSE')
        }
      }
      const initialSample = { time: initial.simTime, outputs: initial.values }
      await deliver(current, [initialSample])
      if (current.inputControllerTimes.length && current.inputControllerTimes[0] <= resumeTime + 1e-12) {
        while (current.inputControllerTimes[current.inputControllerTimeIndex] <= resumeTime + 1e-12) current.inputControllerTimeIndex += 1
      }
      await updateController(current, initialSample, { initial: true })
      onRunAccepted?.({ runId: current.runId })
      if (current.executionPace === 'realtime') {
        current.paceClock = { simTime: resumeTime, wallTime: performance.now() }
      }
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
    captureState,
    requestModelChange(modelId) { return !disposed && postToHost(MESSAGE.MODEL_CHANGE, { modelId }) },
    dispose() {
      disposed = true
      cancel()
      releaseCompletedRun()
      for (const requestId of grants.keys()) finishGrant(requestId, errorWithCode('The client was disposed.', 'DISPOSED'))
      globalThis.window?.removeEventListener('message', receive)
    },
    get hostReady() { return hostReady },
    get activeRunId() { return active?.runId || null },
  }
}
