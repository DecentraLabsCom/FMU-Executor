import {
  createProtocolId, DIGITAL_TWIN_CAPABILITY, DIGITAL_TWIN_MESSAGE as MESSAGE,
  isHostMessageEvent, isValidOptions, isValidParameters,
  isValidSample, isRecord, postToHost, MAX_SAMPLES_PER_BATCH,
} from './index.js'
import { connectFmuSession } from './fmu-client.js'
import {
  createAdaptiveBatchController,
  getAdaptiveBatchSize,
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
const UTF8_ENCODER = new TextEncoder()
const VALID_ADVANCE_STOP_REASONS = new Set([
  'target_reached', 'max_samples', 'compute_budget', 'early_return', 'simulation_finished',
])

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
      return Array.isArray(option) && option.length <= 512
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
    const writesInputs = type === 'sim.setInputs'
    const tracksAdaptiveResponse = run.batchPolicy === 'adaptive' && type === 'sim.advance'
    const operationStartedAt = performance.now()
    let rateLimitWaitMs = 0
    while (true) {
      run.controller.signal.throwIfAborted()
      try {
        if (writesInputs && run.lastInputUpdateAt !== null) {
          const waitMs = run.lastInputUpdateAt + getInputUpdateIntervalMs(run.session.capabilities) - performance.now()
          if (waitMs > 0) await wait(Math.ceil(waitMs), run.controller.signal)
        }
        if (readsOutputs && run.nextOutputAt > performance.now()) {
          const throttleWaitStartedAt = performance.now()
          await wait(Math.ceil(run.nextOutputAt - throttleWaitStartedAt), run.controller.signal)
          rateLimitWaitMs += performance.now() - throttleWaitStartedAt
        }
        const response = await run.session.request(type, expected, payload)
        if (writesInputs) run.lastInputUpdateAt = performance.now()
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
          await wait(inputRetryAfterMs, run.controller.signal)
          continue
        }
        if (error.code !== 'OUTPUT_READ_RATE_LIMITED' || !Number.isFinite(error.retryAfterMs) || error.retryAfterMs < 1 || error.retryAfterMs > 10_000) throw error
        const retryWaitStartedAt = performance.now()
        await wait(error.retryAfterMs, run.controller.signal)
        rateLimitWaitMs += performance.now() - retryWaitStartedAt
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
      await operation(run, 'sim.setInputs', 'sim.inputs.updated', { values })
    }
  }
  async function pump(run) {
    try {
      let time = run.options.startTime
      let eventIndex = 0
      while (time < run.options.stopTime - 1e-12) {
        run.controller.signal.throwIfAborted()
        const scheduleTolerance = Math.max(1e-9, Math.abs(run.options.stepSize) * 1e-7)
        while (eventIndex < run.schedule.length && run.schedule[eventIndex].time <= time + scheduleTolerance) {
          await operation(run, 'sim.setInputs', 'sim.inputs.updated', { values: run.schedule[eventIndex++].values })
        }
        const nextControllerTime = run.inputController
          ? Math.min(
            run.nextControllerTime ?? Infinity,
            run.inputControllerTimes[run.inputControllerTimeIndex] ?? Infinity,
          )
          : Infinity
        const scheduleBatch = run.schedule.length
          ? createScheduledInputBatch(run, eventIndex, Math.min(run.options.stopTime, nextControllerTime))
          : { events: [] }
        const scheduledTargetTime = scheduleBatch.events.length
          ? scheduleBatch.events.at(-1).time
          : Infinity
        let targetTime = Math.min(run.options.stopTime, nextControllerTime, scheduledTargetTime)
        if (!Number.isFinite(targetTime)) targetTime = run.options.stopTime
        if (targetTime <= time + scheduleTolerance) targetTime = Math.min(run.options.stopTime, time + run.options.stepSize)
        const controllerSampleLimit = run.inputController && run.inputControllerInterval
          ? Math.max(1, Math.ceil((targetTime - time) / run.options.stepSize) + 1)
          : 1
        const maxSamples = run.batchPolicy === 'adaptive'
          ? getAdaptiveBatchSize(run.batchController)
          : run.inputController
            ? Math.min(run.maxBatchSamples, controllerSampleLimit)
            : run.maxBatchSamples
        const result = await operation(run, 'sim.advance', 'sim.samples', {
          targetTime,
          maxSamples,
          stepSize: run.options.stepSize,
          ...(scheduleBatch.events.length ? { inputSchedule: scheduleBatch.events } : {}),
          ...run.advanceOptions,
          ...(run.batchPolicy === 'adaptive' ? { maxComputeMs: run.batchController.maxComputeMs } : {}),
        })
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

  async function run({
    parameters = {}, options = {}, inputSchedule, inputController, batchPolicy = 'fixed',
    maxBatchSamples = MAX_SAMPLES_PER_BATCH, adaptiveBatchOptions = {}, advanceOptions = {},
    includeModelVariables = false, validateModel, parametersFromModel,
    inputControllerInterval, inputControllerTimes = [],
  } = {}) {
    if (disposed || active) throw errorWithCode('The app cannot start another FMU run.', 'RUN_UNAVAILABLE')
    if (!isValidParameters(parameters) || !isValidOptions(options)) throw errorWithCode('Invalid FMU run parameters.', 'INVALID_RUN_REQUEST')
    parameters = { ...parameters }
    options = { ...options }
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
    if ((inputControllerInterval !== undefined || inputControllerTimes.length) && !inputController) {
      throw errorWithCode('Input controller timing requires an input controller.', 'INVALID_INPUT')
    }
    if (typeof includeModelVariables !== 'boolean'
      || (validateModel !== undefined && typeof validateModel !== 'function')
      || (parametersFromModel !== undefined && typeof parametersFromModel !== 'function')) {
      throw errorWithCode('Invalid model validation options.', 'INVALID_INPUT')
    }
    if (!['fixed', 'adaptive'].includes(batchPolicy)) throw errorWithCode('Invalid batch policy.', 'INVALID_INPUT')
    if (!isRecord(adaptiveBatchOptions)) throw errorWithCode('Invalid adaptive batch options.', 'INVALID_INPUT')
    if (!isValidAdvanceOptions(advanceOptions)) throw errorWithCode('Invalid FMU advance options.', 'INVALID_INPUT')
    if (!Number.isInteger(maxBatchSamples) || maxBatchSamples < 1 || maxBatchSamples > MAX_SAMPLES_PER_BATCH) throw errorWithCode(`maxBatchSamples must be an integer from 1 to ${MAX_SAMPLES_PER_BATCH}.`, 'INVALID_INPUT')
    if (batchPolicy === 'adaptive' && inputController) throw errorWithCode('Adaptive batching cannot be combined with a per-sample input controller.', 'INVALID_INPUT')
    if (inputController && !inputControllerInterval && maxBatchSamples !== MAX_SAMPLES_PER_BATCH && maxBatchSamples !== 1) throw errorWithCode('maxBatchSamples must be 1 when using a per-sample input controller without an update interval.', 'INVALID_INPUT')
    const current = {
      runId: createProtocolId('run'), controller: new AbortController(), sampleCount: 0,
      options, inputController, inputControllerInterval, inputControllerTimes: [...inputControllerTimes].sort((a, b) => a - b),
      inputControllerTimeIndex: 0, advanceOptions: { ...advanceOptions }, includeModelVariables, validateModel, parametersFromModel,
      batchPolicy, maxBatchSamples, adaptiveBatchOptions,
      lastSampleSequence: null, lastInputUpdateAt: null,
    }
    active = current
    try {
      const grant = await requestConnection(options, current.controller.signal)
      current.controller.signal.throwIfAborted()
      current.session = await connectFmuSession(grant.connection, { signal: current.controller.signal })
      if (current.session.capabilities.advanceSamples !== true) throw errorWithCode('The provider FMU runtime needs batched WebSocket support.', 'UNSUPPORTED_FMU_RUNTIME')
      if (batchPolicy === 'adaptive' && current.session.capabilities.advanceComputeBudget !== true) {
        throw errorWithCode('The provider FMU runtime needs compute-bounded advance support for adaptive batching.', 'UNSUPPORTED_ADAPTIVE_FMU_RUNTIME')
      }
      if (batchPolicy === 'adaptive' && current.session.capabilities.advanceSampleSequences !== true) {
        throw errorWithCode('The provider FMU runtime needs sequenced advance samples for adaptive batching.', 'UNSUPPORTED_ADAPTIVE_FMU_RUNTIME')
      }
      const outputHz = current.session.capabilities.maxOutputHz
      current.outputIntervalMs = Number.isFinite(outputHz) && outputHz >= 1 && outputHz <= 1000 ? 1000 / outputHz : 0
      const model = await current.session.request('model.describe', 'model.description', { includeVariables: includeModelVariables })
      if (validateModel) {
        const validModel = await validateModel(model)
        if (validModel === false) throw errorWithCode('The connected FMU does not match this application.', 'FMU_MODEL_MISMATCH')
      }
      const modelParameters = parametersFromModel ? await parametersFromModel(model) : {}
      if (!isValidParameters(modelParameters)) throw errorWithCode('The FMU model produced invalid initialization parameters.', 'INVALID_INPUT')
      const runParameters = { ...modelParameters, ...parameters }
      if (!isValidParameters(runParameters)) throw errorWithCode('The FMU initialization parameters exceed the supported limits.', 'INVALID_INPUT')
      current.options = {
        startTime: options.startTime ?? model.defaultStartTime ?? 0,
        stopTime: options.stopTime ?? model.defaultStopTime ?? 1,
        stepSize: options.stepSize ?? model.defaultStepSize ?? 0.001,
      }
      const { startTime, stopTime, stepSize } = current.options
      if (!isValidOptions(current.options) || stopTime <= startTime || stepSize <= 0) throw errorWithCode('Invalid simulation interval.', 'INVALID_RUN_REQUEST')
      if (current.inputControllerTimes.some((time) => time < startTime || time > stopTime)) {
        throw errorWithCode('An input controller time is outside the simulation interval.', 'INVALID_INPUT')
      }
      current.nextControllerTime = inputControllerInterval === undefined ? Infinity : startTime + inputControllerInterval
      if (batchPolicy === 'adaptive') {
        current.batchController = createAdaptiveBatchController({
          startTime, stopTime, stepSize, maxSamples: maxBatchSamples, ...adaptiveBatchOptions,
        })
      }
      const schedule = inputSchedule ?? grant.inputSchedule ?? []
      if (!isValidRunInputSchedule(schedule) || schedule.some((event) => event.time < startTime || event.time > stopTime)) throw errorWithCode('Invalid input schedule.', 'INVALID_INPUT_SCHEDULE')
      if (schedule.length && current.session.capabilities.advanceInputValues !== true) {
        throw errorWithCode('The provider FMU runtime cannot apply scheduled inputs during advance.', 'UNSUPPORTED_SCHEDULED_INPUTS')
      }
      current.scheduleQuota = schedule.length ? getInputUpdateQuota(current.session.capabilities) : null
      current.schedule = [...schedule].sort((a, b) => a.time - b.time)
      await current.session.request('sim.initialize', 'sim.state', { parameters: runParameters, options: { ...current.options, timeMode: 'simtime' } })
      onReset?.(current.runId)
      state(current, 'running')
      const initial = await operation(current, 'sim.getOutputs', 'sim.outputs', {
        ...(current.advanceOptions.outputVariables ? { variables: current.advanceOptions.outputVariables } : {}),
      })
      const initialSample = { time: initial.simTime, outputs: initial.values }
      if (onSamples && onSample) await onSample(initialSample, { runId: current.runId, signal: current.controller.signal })
      await deliver(current, [initialSample])
      if (current.inputControllerTimes.length && current.inputControllerTimes[0] <= startTime + 1e-12) {
        while (current.inputControllerTimes[current.inputControllerTimeIndex] <= startTime + 1e-12) current.inputControllerTimeIndex += 1
      }
      await updateController(current, initialSample, { initial: true })
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
