// Host integration v2 carries authorization and optional FMI-LS controls.
export { createFmiDependencyMetadata, getDependentOutputRecommendations } from './model-structure.js'
export { resolveEffectiveTolerance } from './simulation-options.js'
export const DIGITAL_TWIN_PROTOCOL_VERSION = 2
export const DIGITAL_TWIN_APP_ASSET_FILENAME = 'index.js'
export const MAX_RUN_PARAMETERS = 256
export const MAX_PARAMETER_NAME_LENGTH = 256
export const MAX_PARAMETER_STRING_LENGTH = 1024
export const MAX_OUTPUTS_PER_SAMPLE = 512
export const MAX_SAMPLES_PER_BATCH = 256
export const MAX_SAMPLE_BATCH_JSON_CHARS = 1_000_000
export const MAX_MODEL_DESCRIPTION_JSON_BYTES = 32 * 1024 * 1024
export const DIGITAL_TWIN_MESSAGE = Object.freeze({
  HOST_READY: 'decentralabs:digital-twin-host-ready',
  APP_READY: 'decentralabs:digital-twin-ready',
  APP_HELLO: 'decentralabs:digital-twin-connect',
  APP_ERROR: 'decentralabs:digital-twin-error',
  CONNECTION_REQUEST: 'decentralabs:fmu-connection-request',
  CONNECTION_READY: 'decentralabs:fmu-connection-ready',
  CONNECTION_ERROR: 'decentralabs:fmu-connection-error',
  BATCH_EXECUTION_REQUEST: 'decentralabs:batch-execution-request',
  BATCH_EXECUTION_CANCEL: 'decentralabs:batch-execution-cancel',
  BATCH_EXECUTION_ACK: 'decentralabs:batch-execution-ack',
  BATCH_EXECUTION_EVENT: 'decentralabs:batch-execution-event',
  BATCH_EXECUTION_END: 'decentralabs:batch-execution-end',
  EXECUTION_STATE: 'decentralabs:execution-state',
  EXECUTION_CANCEL: 'decentralabs:execution-cancel',
  PLAYBACK_CONTROL: 'decentralabs:fmi-playback-control',
  FMI_LS_PARAMETER_PRESET: 'decentralabs:fmi-ls-parameter-preset',
  FMI_LS_EXPERIMENT: 'decentralabs:fmi-ls-experiment',
  FMU_STATE_CONFIGURATION: 'decentralabs:fmu-state-configuration',
  FMU_STATE_CAPTURE: 'decentralabs:fmu-state-capture',
  FMU_STATE_SAVED: 'decentralabs:fmu-state-saved',
  FMU_STATE_ERROR: 'decentralabs:fmu-state-error',
  MODEL_CHANGE: 'decentralabs:digital-twin-model-change',
})
export const DIGITAL_TWIN_CAPABILITY = Object.freeze({
  PLAYBACK_RATE: 'playback-rate',
  EXECUTION_PACE: 'execution-pace',
  EXECUTION_CONTROL: 'execution-control',
  FMI_LS_PARAMETER_PRESETS: 'fmi-ls-parameter-presets',
  FMI_LS_EXPERIMENTS: 'fmi-ls-experiments',
  FMU_STATE_CHECKPOINTS: 'fmu-state-checkpoints',
})
export const APP_TO_HOST_MESSAGE_TYPES = Object.freeze([
  DIGITAL_TWIN_MESSAGE.APP_HELLO,
  DIGITAL_TWIN_MESSAGE.APP_READY, DIGITAL_TWIN_MESSAGE.APP_ERROR, DIGITAL_TWIN_MESSAGE.CONNECTION_REQUEST,
  DIGITAL_TWIN_MESSAGE.EXECUTION_STATE, DIGITAL_TWIN_MESSAGE.MODEL_CHANGE,
  DIGITAL_TWIN_MESSAGE.FMU_STATE_SAVED, DIGITAL_TWIN_MESSAGE.FMU_STATE_ERROR,
  DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_REQUEST, DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_CANCEL,
  DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_ACK,
])
export const HOST_TO_APP_MESSAGE_TYPES = Object.freeze([
  DIGITAL_TWIN_MESSAGE.HOST_READY, DIGITAL_TWIN_MESSAGE.CONNECTION_READY, DIGITAL_TWIN_MESSAGE.CONNECTION_ERROR,
  DIGITAL_TWIN_MESSAGE.EXECUTION_CANCEL, DIGITAL_TWIN_MESSAGE.PLAYBACK_CONTROL,
  DIGITAL_TWIN_MESSAGE.FMI_LS_PARAMETER_PRESET, DIGITAL_TWIN_MESSAGE.FMI_LS_EXPERIMENT,
  DIGITAL_TWIN_MESSAGE.FMU_STATE_CONFIGURATION, DIGITAL_TWIN_MESSAGE.FMU_STATE_CAPTURE,
  DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_EVENT, DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_END,
])
export const isRecord = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const keys = (v, allowed) => Object.keys(v).every((key) => allowed.includes(key))
const text = (v, max = 128) => typeof v === 'string' && v.length > 0 && v.length <= max
  && Array.from(v).every((character) => character.codePointAt(0) > 31 && character.codePointAt(0) !== 127)
export const isValidProtocolId = (v) => text(v) && /^[a-zA-Z0-9][a-zA-Z0-9._:-]*$/.test(v)
export const isProtocolEnvelope = (v) => isRecord(v) && v.version === DIGITAL_TWIN_PROTOCOL_VERSION && typeof v.type === 'string'
export const isFiniteScalar = (v) => typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v)) || (typeof v === 'string' && v.length <= 1024)
export const isValidOptions = (v) => isRecord(v) && Object.entries(v).every(([key, value]) => (
  ['startTime', 'stopTime', 'stepSize', 'tolerance'].includes(key)
  && typeof value === 'number' && Number.isFinite(value)
  && (key !== 'tolerance' || value > 0)
))
export const isValidParameters = (v) => isRecord(v) && Object.keys(v).length <= MAX_RUN_PARAMETERS && Object.entries(v).every(([key, value]) => text(key, 256) && isFiniteScalar(value))
export const isValidInputSchedule = (v) => Array.isArray(v) && v.length <= 128 && v.every((event) => isRecord(event) && keys(event, ['time', 'values']) && Number.isFinite(event.time) && isValidParameters(event.values))
export function isValidFmuStateCheckpoint(v) {
  return isRecord(v) && keys(v, ['format', 'checkpointId', 'scope', 'createdAt', 'expiresAt', 'signature'])
    && v.format === 'decentralabs-fmi-state-v1'
    && typeof v.checkpointId === 'string' && /^[a-f\d]{32}$/.test(v.checkpointId)
    && typeof v.scope === 'string' && /^[a-f\d]{64}$/.test(v.scope)
    && Number.isFinite(v.createdAt) && Number.isFinite(v.expiresAt)
    && v.expiresAt > v.createdAt && v.expiresAt > Date.now() / 1000
    && typeof v.signature === 'string' && /^[a-f\d]{64}$/.test(v.signature)
}
export function isValidFmuStateCheckpointBundle(v) {
  if (!isRecord(v) || !keys(v, ['version', 'simTime', 'checkpoint', 'sample', 'modelValues']) || v.version !== 1
    || !Number.isFinite(v.simTime) || !isValidFmuStateCheckpoint(v.checkpoint)) return false
  if (v.sample !== undefined && (!isRecord(v.sample) || !keys(v.sample, ['time', 'outputs'])
    || !Number.isFinite(v.sample.time) || !isRecord(v.sample.outputs)
    || Object.keys(v.sample.outputs).length > MAX_OUTPUTS_PER_SAMPLE
    || !Object.entries(v.sample.outputs).every(([name, value]) => text(name, 256)
      && (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value)))))) return false
  if (v.modelValues !== undefined && !isValidParameters(v.modelValues)) return false
  try { return new TextEncoder().encode(JSON.stringify(v)).length <= 4096 } catch { return false }
}
export function isValidConnectionConfig(v) {
  if (!isRecord(v) || !keys(v, ['protocolVersion', 'gatewayWsUrl', 'labId', 'reservationKey', 'sessionTicket', 'ticketExpiresAt'])) return false
  if (v.protocolVersion !== '1.0' || !text(v.labId) || !text(v.reservationKey, 256) || !text(v.sessionTicket, 4096) || !Number.isSafeInteger(v.ticketExpiresAt) || v.ticketExpiresAt <= 0) return false
  try {
    const url = new URL(v.gatewayWsUrl)
    return ['wss:', 'ws:'].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash
      && (url.protocol === 'wss:' || ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))
  } catch { return false }
}
export function isValidSample(v) {
  return isRecord(v) && keys(v, ['time', 'outputs', 'sequence', 'eventOccurred', 'eventHandlingNeeded', 'earlyReturn', 'terminateSimulation', 'finished', 'finishReason'])
    && Number.isFinite(v.time) && isRecord(v.outputs)
    && Object.keys(v.outputs).length <= MAX_OUTPUTS_PER_SAMPLE
    && Object.entries(v.outputs).every(([key, value]) => text(key, 256) && (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))))
    && (v.sequence === undefined || (Number.isSafeInteger(v.sequence) && v.sequence > 0))
    && ['eventOccurred', 'eventHandlingNeeded', 'earlyReturn', 'terminateSimulation', 'finished'].every((key) => v[key] === undefined || typeof v[key] === 'boolean')
    && (v.finishReason === undefined || text(v.finishReason))
}
export function isValidAppReadyMessage(v) {
  const capabilities = Array.isArray(v?.capabilities) ? v.capabilities : []
  return isProtocolEnvelope(v) && v.type === DIGITAL_TWIN_MESSAGE.APP_READY
    && keys(v, ['type', 'version', 'capabilities', 'defaultPlaybackRate', 'defaultExecutionPace', 'playbackControlPosition'])
    && (v.capabilities === undefined || (Array.isArray(v.capabilities) && v.capabilities.length <= 32 && new Set(v.capabilities).size === v.capabilities.length && v.capabilities.every((c) => text(c))))
    && (v.defaultPlaybackRate === undefined || (Number.isFinite(v.defaultPlaybackRate) && v.defaultPlaybackRate > 0 && v.defaultPlaybackRate <= 100000))
    && (v.defaultExecutionPace === undefined || (['fastest', 'realtime'].includes(v.defaultExecutionPace) && capabilities.includes(DIGITAL_TWIN_CAPABILITY.EXECUTION_PACE)))
    && (v.playbackControlPosition === undefined || ['top-left', 'top-right', 'bottom-left', 'bottom-right'].includes(v.playbackControlPosition))
}
function isValidFmiModelMetadata(v) {
  if (!isRecord(v) || !keys(v, ['modelVariables', 'modelStructure'])
    || !Array.isArray(v.modelVariables) || v.modelVariables.length > 4096
    || !isRecord(v.modelStructure) || !keys(v.modelStructure, ['outputs'])
    || !Array.isArray(v.modelStructure.outputs) || v.modelStructure.outputs.length > 4096) return false
  const variableNames = new Map()
  for (const variable of v.modelVariables) {
    if (!isRecord(variable)
      || !keys(variable, ['name', 'causality', 'type', 'variability', 'description', 'quantity', 'unit', 'displayUnit'])
      || !text(variable.name, 512) || !['input', 'output'].includes(variable.causality)) return false
    for (const [field, limit] of Object.entries({ type: 64, variability: 32, description: 512, quantity: 256, unit: 128, displayUnit: 128 })) {
      if (variable[field] !== undefined && !text(variable[field], limit)) return false
    }
    variableNames.set(variable.name, variable.causality)
  }
  let dependencyCount = 0
  for (const output of v.modelStructure.outputs) {
    if (!isRecord(output) || !keys(output, ['name', 'dependencyNames'])
      || variableNames.get(output.name) !== 'output' || !Array.isArray(output.dependencyNames)
      || output.dependencyNames.length > 256 || !output.dependencyNames.length
      || !output.dependencyNames.every((name) => variableNames.get(name) === 'input')) return false
    dependencyCount += output.dependencyNames.length
    if (dependencyCount > 8192) return false
  }
  try { return new TextEncoder().encode(JSON.stringify(v)).length <= 8 * 1024 * 1024 } catch { return false }
}
export const isValidAppErrorMessage = (v) => isProtocolEnvelope(v) && v.type === DIGITAL_TWIN_MESSAGE.APP_ERROR && keys(v, ['type', 'version', 'code']) && (v.code === undefined || text(v.code))
export function isValidAppMessage(v) {
  if (!isProtocolEnvelope(v)) return false
  switch (v.type) {
    case DIGITAL_TWIN_MESSAGE.APP_HELLO: return keys(v, ['type', 'version'])
    case DIGITAL_TWIN_MESSAGE.APP_READY: return isValidAppReadyMessage(v)
    case DIGITAL_TWIN_MESSAGE.APP_ERROR: return isValidAppErrorMessage(v)
    case DIGITAL_TWIN_MESSAGE.CONNECTION_REQUEST:
      return keys(v, ['type', 'version', 'requestId', 'options', 'transport', 'executionPace']) && isValidProtocolId(v.requestId)
        && (v.options === undefined || isValidOptions(v.options))
        && (v.transport === undefined || ['websocket', 'batch-http'].includes(v.transport))
        && (v.executionPace === undefined || ['fastest', 'realtime'].includes(v.executionPace))
    case DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_REQUEST:
      return keys(v, ['type', 'version', 'requestId', 'runId', 'body', 'batchOptions'])
        && isValidProtocolId(v.requestId) && isValidProtocolId(v.runId)
        && isValidBatchSimulationBody(v.body) && isValidBatchOptions(v.batchOptions)
    case DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_CANCEL:
      return keys(v, ['type', 'version', 'requestId']) && isValidProtocolId(v.requestId)
    case DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_ACK:
      return keys(v, ['type', 'version', 'requestId', 'eventId']) && isValidProtocolId(v.requestId)
        && Number.isSafeInteger(v.eventId) && v.eventId >= 1
    case DIGITAL_TWIN_MESSAGE.EXECUTION_STATE:
      return keys(v, ['type', 'version', 'runId', 'state', 'options']) && isValidProtocolId(v.runId)
        && ['idle', 'running', 'completed', 'cancelled', 'error'].includes(v.state) && (v.options === undefined || isValidOptions(v.options))
    case DIGITAL_TWIN_MESSAGE.MODEL_CHANGE:
      return keys(v, ['type', 'version', 'modelId']) && text(v.modelId)
    case DIGITAL_TWIN_MESSAGE.FMU_STATE_SAVED:
      return keys(v, ['type', 'version', 'bundle']) && isValidFmuStateCheckpointBundle(v.bundle)
    case DIGITAL_TWIN_MESSAGE.FMU_STATE_ERROR:
      return keys(v, ['type', 'version', 'code']) && text(v.code)
    default: return false
  }
}
export function isValidHostMessage(v) {
  if (!isProtocolEnvelope(v)) return false
  switch (v.type) {
    case DIGITAL_TWIN_MESSAGE.HOST_READY:
      return keys(v, ['type', 'version', 'extensions']) && (v.extensions === undefined || (isRecord(v.extensions)
        && Object.keys(v.extensions).length <= 32
        && (v.extensions.fmiModelMetadata === undefined || isValidFmiModelMetadata(v.extensions.fmiModelMetadata))))
    case DIGITAL_TWIN_MESSAGE.CONNECTION_READY:
      return keys(v, ['type', 'version', 'requestId', 'transport', 'connection', 'inputSchedule', 'executionPace', 'defaultStartTime', 'defaultStepSize', 'captureFmuStateAvailable'])
        && isValidProtocolId(v.requestId)
        && (v.transport === undefined || ['websocket', 'batch-http'].includes(v.transport))
        && (v.transport === 'batch-http'
          ? v.connection === undefined
          : isValidConnectionConfig(v.connection))
        && (v.inputSchedule === undefined || isValidInputSchedule(v.inputSchedule))
        && (v.executionPace === undefined || ['fastest', 'realtime'].includes(v.executionPace))
        && (v.defaultStartTime === undefined || Number.isFinite(v.defaultStartTime))
        && (v.defaultStepSize === undefined || (Number.isFinite(v.defaultStepSize) && v.defaultStepSize > 0))
        && (v.captureFmuStateAvailable === undefined || typeof v.captureFmuStateAvailable === 'boolean')
    case DIGITAL_TWIN_MESSAGE.CONNECTION_ERROR:
      return keys(v, ['type', 'version', 'requestId', 'code']) && isValidProtocolId(v.requestId) && text(v.code)
    case DIGITAL_TWIN_MESSAGE.EXECUTION_CANCEL:
      return keys(v, ['type', 'version', 'runId']) && isValidProtocolId(v.runId)
    case DIGITAL_TWIN_MESSAGE.PLAYBACK_CONTROL:
      return keys(v, ['type', 'version', 'rate', 'rateUnit']) && Number.isFinite(v.rate) && v.rate > 0 && v.rate <= 100000 && v.rateUnit === 'model-seconds-per-real-second'
    case DIGITAL_TWIN_MESSAGE.FMI_LS_PARAMETER_PRESET:
      return keys(v, ['type', 'version', 'id', 'name', 'description', 'parameters'])
        && isValidProtocolId(v.id) && text(v.name, 180)
        && typeof v.description === 'string' && v.description.length <= 1024
        && isValidParameters(v.parameters)
    case DIGITAL_TWIN_MESSAGE.FMI_LS_EXPERIMENT:
      return keys(v, ['type', 'version', 'experiment'])
        && (v.experiment === null || isValidFmiLsExperiment(v.experiment))
    case DIGITAL_TWIN_MESSAGE.FMU_STATE_CONFIGURATION:
      return keys(v, ['type', 'version', 'checkpoint', 'resume', 'captureOnCompletion'])
        && (v.checkpoint === null || isValidFmuStateCheckpointBundle(v.checkpoint))
        && typeof v.resume === 'boolean' && typeof v.captureOnCompletion === 'boolean'
    case DIGITAL_TWIN_MESSAGE.FMU_STATE_CAPTURE:
      return keys(v, ['type', 'version'])
    case DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_EVENT:
      return keys(v, ['type', 'version', 'requestId', 'eventId', 'event']) && isValidProtocolId(v.requestId)
        && Number.isSafeInteger(v.eventId) && v.eventId >= 1
        && isValidBatchSimulationEvent(v.event)
    case DIGITAL_TWIN_MESSAGE.BATCH_EXECUTION_END:
      return keys(v, ['type', 'version', 'requestId', 'code']) && isValidProtocolId(v.requestId)
        && (v.code === undefined || text(v.code))
    default: return false
  }
}
export function isValidBatchSimulationBody(v) {
  return isRecord(v)
    && keys(v, ['parameters', 'options', 'inputSchedule', 'checkpoint', 'captureFmuState'])
    && isValidParameters(v.parameters)
    && isRecord(v.options)
    && keys(v.options, ['startTime', 'stopTime', 'stepSize', 'tolerance'])
    && Number.isFinite(v.options.startTime) && Number.isFinite(v.options.stopTime)
    && v.options.stopTime > v.options.startTime
    && Number.isFinite(v.options.stepSize) && v.options.stepSize > 0
    && (v.options.tolerance === undefined || (Number.isFinite(v.options.tolerance) && v.options.tolerance > 0))
    && (v.inputSchedule === undefined || isValidInputSchedule(v.inputSchedule))
    && (v.checkpoint === undefined || isValidFmuStateCheckpoint(v.checkpoint))
    && (v.captureFmuState === undefined || typeof v.captureFmuState === 'boolean')
}
export function isValidBatchOptions(v) {
  return isRecord(v) && keys(v, ['maxSamples', 'outputVariables', 'outputSampleEverySteps'])
    && Number.isInteger(v.maxSamples) && v.maxSamples >= 1 && v.maxSamples <= MAX_SAMPLES_PER_BATCH
    && (v.outputVariables === undefined || (Array.isArray(v.outputVariables)
      && v.outputVariables.length > 0 && v.outputVariables.length <= MAX_OUTPUTS_PER_SAMPLE
      && new Set(v.outputVariables).size === v.outputVariables.length
      && v.outputVariables.every((name) => text(name, 256))))
    && (v.outputSampleEverySteps === undefined || (Number.isInteger(v.outputSampleEverySteps)
      && v.outputSampleEverySteps >= 1 && v.outputSampleEverySteps <= 100000))
}
export function isValidBatchSimulationEvent(v) {
  if (!isRecord(v) || typeof v.kind !== 'string') return false
  if (v.kind === 'samples') {
    if (!keys(v, ['kind', 'samples']) || !Array.isArray(v.samples) || !v.samples.length
      || v.samples.length > MAX_SAMPLES_PER_BATCH || !v.samples.every(isValidSample)) return false
    try { return new TextEncoder().encode(JSON.stringify(v)).length <= MAX_SAMPLE_BATCH_JSON_CHARS } catch { return false }
  }
  if (v.kind === 'completed') {
    return keys(v, ['kind', 'time', 'finishReason', 'checkpoint', 'checkpointTime', 'checkpointSample'])
      && Number.isFinite(v.time)
      && (v.finishReason === undefined || text(v.finishReason))
      && (v.checkpoint === undefined || isValidFmuStateCheckpoint(v.checkpoint))
      && (v.checkpointTime === undefined || Number.isFinite(v.checkpointTime))
      && (v.checkpointSample === undefined || isValidSample(v.checkpointSample))
  }
  return v.kind === 'error' && keys(v, ['kind', 'code', 'message']) && text(v.code)
    && (v.message === undefined || text(v.message, 512))
}
export function isValidFmiLsExperiment(v) {
  return isRecord(v)
    && keys(v, ['id', 'name', 'description', 'parameters', 'options', 'inputSchedule'])
    && isValidProtocolId(v.id)
    && text(v.name, 180)
    && typeof v.description === 'string' && v.description.length <= 2048
    && isValidParameters(v.parameters)
    && isValidOptions(v.options)
    && (v.inputSchedule === undefined || isValidInputSchedule(v.inputSchedule))
    && (Object.keys(v.parameters).length > 0 || Object.keys(v.options).length > 0 || (v.inputSchedule?.length || 0) > 0)
}
export function createProtocolId(prefix = 'run') {
  return `${prefix}:${globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 14)}`}`
}
export const makeProtocolMessage = (type, payload = {}) => ({ ...payload, type, version: DIGITAL_TWIN_PROTOCOL_VERSION })
export const isHostMessageEvent = (event) => typeof window !== 'undefined' && event?.source === window.parent && isValidHostMessage(event.data)
export function postToHost(type, payload = {}) {
  if (typeof window === 'undefined' || window.parent === window) return false
  window.parent.postMessage(makeProtocolMessage(type, payload), '*')
  return true
}
