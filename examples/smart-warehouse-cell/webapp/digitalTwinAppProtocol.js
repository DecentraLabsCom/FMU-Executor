export const DIGITAL_TWIN_PROTOCOL_VERSION = 1
export const MAX_RUN_PARAMETERS = 256
export const MAX_PARAMETER_NAME_LENGTH = 256
export const MAX_PARAMETER_STRING_LENGTH = 1024
export const MAX_OUTPUTS_PER_SAMPLE = 512
export const MAX_SAMPLES_PER_BATCH = 256
export const MAX_SAMPLE_BATCH_JSON_CHARS = 1_000_000
export const SAMPLE_ACK_TIMEOUT_MS = 15_000

export const DIGITAL_TWIN_MESSAGE = Object.freeze({
  HOST_READY: 'decentralabs:digital-twin-host-ready',
  APP_READY: 'decentralabs:digital-twin-ready',
  APP_ERROR: 'decentralabs:digital-twin-error',
  RUN_REQUEST: 'decentralabs:fmi-run-request',
  RUN_ACCEPTED: 'decentralabs:fmi-run-accepted',
  RUN_REJECTED: 'decentralabs:fmi-run-rejected',
  RUN_CANCEL: 'decentralabs:fmi-run-cancel',
  RUN_CANCELLED: 'decentralabs:fmi-run-cancelled',
  SAMPLE: 'decentralabs:fmi-sample',
  SAMPLES: 'decentralabs:fmi-samples',
  SAMPLES_ACK: 'decentralabs:fmi-samples-ack',
  RESET: 'decentralabs:fmi-reset',
  SIMULATION_STATE: 'decentralabs:fmi-simulation-state',
  PLAYBACK_CONTROL: 'decentralabs:fmi-playback-control',
  MODEL_CHANGE: 'decentralabs:digital-twin-model-change',
})

export const DIGITAL_TWIN_CAPABILITY = Object.freeze({
  PLAYBACK_RATE: 'playback-rate',
  SAMPLE_ACKNOWLEDGEMENT: 'sample-acknowledgement',
})

const RUN_OPTIONS = new Set(['startTime', 'stopTime', 'stepSize'])

function hasOnlyKeys(value, keys) {
  const allowed = new Set(keys)
  return Object.keys(value).every((key) => allowed.has(key))
}

function hasControlCharacters(value) {
  for (const character of value) {
    const codePoint = character.codePointAt(0)
    if (codePoint <= 31 || codePoint === 127) return true
  }
  return false
}

export function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function isProtocolEnvelope(value) {
  return isRecord(value)
    && value.version === DIGITAL_TWIN_PROTOCOL_VERSION
    && typeof value.type === 'string'
}

export function isValidProtocolId(value) {
  return typeof value === 'string'
    && value.length >= 1
    && value.length <= 128
    && /^[a-zA-Z0-9][a-zA-Z0-9._:-]*$/.test(value)
}

export function isValidAppReadyMessage(message) {
  if (!isProtocolEnvelope(message) || message.type !== DIGITAL_TWIN_MESSAGE.APP_READY) return false
  if (!hasOnlyKeys(message, ['type', 'version', 'capabilities', 'defaultPlaybackRate', 'playbackControlPosition'])) return false
  if (message.capabilities !== undefined && (
    !Array.isArray(message.capabilities)
    || message.capabilities.length > 32
    || new Set(message.capabilities).size !== message.capabilities.length
    || Array.from(message.capabilities).some((capability) => (
      typeof capability !== 'string'
      || capability.length === 0
      || capability.length > 128
      || hasControlCharacters(capability)
    ))
  )) return false
  if (message.defaultPlaybackRate !== undefined && (
    typeof message.defaultPlaybackRate !== 'number'
    || !Number.isFinite(message.defaultPlaybackRate)
    || message.defaultPlaybackRate <= 0
    || message.defaultPlaybackRate > 100_000
  )) return false
  return message.playbackControlPosition === undefined
    || ['top-left', 'top-right', 'bottom-left', 'bottom-right'].includes(message.playbackControlPosition)
}

export function isValidAppErrorMessage(message) {
  return isProtocolEnvelope(message)
    && message.type === DIGITAL_TWIN_MESSAGE.APP_ERROR
    && hasOnlyKeys(message, ['type', 'version', 'code'])
    && (message.code === undefined || (typeof message.code === 'string' && message.code.length <= 128))
}

export function createProtocolId(prefix = 'run') {
  const id = globalThis.crypto?.randomUUID?.()
  if (id) return `${prefix}:${id}`
  return `${prefix}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 14)}`
}

export function isFiniteScalar(value) {
  return typeof value === 'boolean'
    || (typeof value === 'number' && Number.isFinite(value))
    || (typeof value === 'string' && value.length <= MAX_PARAMETER_STRING_LENGTH)
}

export function isValidRunRequest(message) {
  if (!isProtocolEnvelope(message) || message.type !== DIGITAL_TWIN_MESSAGE.RUN_REQUEST) return false
  if (!hasOnlyKeys(message, ['type', 'version', 'requestId', 'parameters', 'options'])) return false
  if (message.requestId !== undefined && !isValidProtocolId(message.requestId)) return false
  if (message.parameters !== undefined) {
    if (!isRecord(message.parameters)) return false
    const entries = Object.entries(message.parameters)
    if (entries.length > MAX_RUN_PARAMETERS) return false
    if (entries.some(([name, value]) => (
      !name || name.length > MAX_PARAMETER_NAME_LENGTH || hasControlCharacters(name) || !isFiniteScalar(value)
    ))) return false
  }
  if (message.options !== undefined) {
    if (!isRecord(message.options)) return false
    const entries = Object.entries(message.options)
    if (entries.some(([name, value]) => !RUN_OPTIONS.has(name) || typeof value !== 'number' || !Number.isFinite(value))) return false
  }
  return true
}

export function isValidSamplesAcknowledgement(message) {
  return isProtocolEnvelope(message)
    && message.type === DIGITAL_TWIN_MESSAGE.SAMPLES_ACK
    && hasOnlyKeys(message, ['type', 'version', 'runId', 'batchId', 'lastSampleIndex'])
    && isValidProtocolId(message.runId)
    && isValidProtocolId(message.batchId)
    && Number.isSafeInteger(message.lastSampleIndex)
    && message.lastSampleIndex >= 0
}

export function isValidRunCancellation(message) {
  return isProtocolEnvelope(message)
    && message.type === DIGITAL_TWIN_MESSAGE.RUN_CANCEL
    && hasOnlyKeys(message, ['type', 'version', 'runId'])
    && isValidProtocolId(message.runId)
}

export function isValidModelChangeRequest(message) {
  return isProtocolEnvelope(message)
    && message.type === DIGITAL_TWIN_MESSAGE.MODEL_CHANGE
    && hasOnlyKeys(message, ['type', 'version', 'modelId'])
    && typeof message.modelId === 'string'
    && message.modelId.length > 0
    && message.modelId.length <= 128
}

export function isValidSample(sample) {
  if (!isRecord(sample) || !hasOnlyKeys(sample, ['time', 'outputs'])) return false
  if (typeof sample.time !== 'number' || !Number.isFinite(sample.time)) return false
  if (!isRecord(sample.outputs)) return false
  const entries = Object.entries(sample.outputs)
  return entries.length <= MAX_OUTPUTS_PER_SAMPLE
    && entries.every(([name, value]) => (
      name.length > 0
      && name.length <= MAX_PARAMETER_NAME_LENGTH
      && !hasControlCharacters(name)
      && (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value)))
    ))
}

function isValidOutputs(outputs) {
  if (!isRecord(outputs)) return false
  const entries = Object.entries(outputs)
  return entries.length <= MAX_OUTPUTS_PER_SAMPLE
    && entries.every(([name, value]) => (
      name.length > 0
      && name.length <= MAX_PARAMETER_NAME_LENGTH
      && !hasControlCharacters(name)
      && (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value)))
    ))
}

export function isValidSamplesBatch(message) {
  if (!isProtocolEnvelope(message) || message.type !== DIGITAL_TWIN_MESSAGE.SAMPLES) return false
  if (!hasOnlyKeys(message, ['type', 'version', 'runId', 'batchId', 'firstSampleIndex', 'lastSampleIndex', 'samples'])) return false
  if (!Array.isArray(message.samples) || message.samples.length === 0 || message.samples.length > MAX_SAMPLES_PER_BATCH) return false
  if (!Array.from(message.samples).every(isValidSample)) return false
  if (message.runId !== undefined && !isValidProtocolId(message.runId)) return false
  if (message.batchId !== undefined && !isValidProtocolId(message.batchId)) return false
  if (message.firstSampleIndex !== undefined && (!Number.isSafeInteger(message.firstSampleIndex) || message.firstSampleIndex < 0)) return false
  if (message.lastSampleIndex !== undefined && (!Number.isSafeInteger(message.lastSampleIndex) || message.lastSampleIndex < 0)) return false
  const hasBatchMetadata = message.batchId !== undefined
    || message.firstSampleIndex !== undefined
    || message.lastSampleIndex !== undefined
  if (hasBatchMetadata && (
    !isValidProtocolId(message.runId)
    || !isValidProtocolId(message.batchId)
    || !Number.isSafeInteger(message.firstSampleIndex)
    || !Number.isSafeInteger(message.lastSampleIndex)
  )) return false
  if (message.firstSampleIndex !== undefined && message.lastSampleIndex !== undefined
      && message.lastSampleIndex - message.firstSampleIndex !== message.samples.length - 1) return false
  try {
    if (JSON.stringify(message).length > MAX_SAMPLE_BATCH_JSON_CHARS) return false
  } catch {
    return false
  }
  return true
}

export function makeProtocolMessage(type, payload = {}) {
  return { ...payload, type, version: DIGITAL_TWIN_PROTOCOL_VERSION }
}

export function isValidHostMessage(message) {
  if (!isProtocolEnvelope(message)) return false
  switch (message.type) {
    case DIGITAL_TWIN_MESSAGE.HOST_READY:
      return hasOnlyKeys(message, ['type', 'version', 'extensions'])
        && (message.extensions === undefined || (isRecord(message.extensions) && Object.keys(message.extensions).length <= 32))
    case DIGITAL_TWIN_MESSAGE.RUN_ACCEPTED:
      return hasOnlyKeys(message, ['type', 'version', 'requestId', 'runId'])
        && isValidProtocolId(message.requestId)
        && isValidProtocolId(message.runId)
    case DIGITAL_TWIN_MESSAGE.RUN_REJECTED:
      return hasOnlyKeys(message, ['type', 'version', 'requestId', 'code'])
        && (message.requestId === undefined || isValidProtocolId(message.requestId))
        && typeof message.code === 'string'
        && message.code.length <= 128
    case DIGITAL_TWIN_MESSAGE.RUN_CANCELLED:
      return hasOnlyKeys(message, ['type', 'version', 'runId']) && isValidProtocolId(message.runId)
    case DIGITAL_TWIN_MESSAGE.SAMPLE:
      return hasOnlyKeys(message, ['type', 'version', 'runId', 'time', 'outputs'])
        && (message.runId === undefined || isValidProtocolId(message.runId))
        && (message.time === null || (typeof message.time === 'number' && Number.isFinite(message.time)))
        && isValidOutputs(message.outputs)
    case DIGITAL_TWIN_MESSAGE.SAMPLES:
      return isValidSamplesBatch(message)
    case DIGITAL_TWIN_MESSAGE.RESET:
      return hasOnlyKeys(message, ['type', 'version', 'runId'])
        && (message.runId === undefined || message.runId === null || isValidProtocolId(message.runId))
    case DIGITAL_TWIN_MESSAGE.SIMULATION_STATE:
      return hasOnlyKeys(message, ['type', 'version', 'runId', 'requestId', 'state'])
        && ['idle', 'running', 'completed', 'cancelled', 'error'].includes(message.state)
        && (message.runId === undefined || isValidProtocolId(message.runId))
        && (message.requestId === undefined || isValidProtocolId(message.requestId))
    case DIGITAL_TWIN_MESSAGE.PLAYBACK_CONTROL:
      return hasOnlyKeys(message, ['type', 'version', 'rate', 'rateUnit'])
        && typeof message.rate === 'number'
        && Number.isFinite(message.rate)
        && message.rate > 0
        && message.rate <= 100000
        && message.rateUnit === 'model-seconds-per-real-second'
    default:
      return false
  }
}

export function isHostMessageEvent(event) {
  return typeof window !== 'undefined'
    && event?.source === window.parent
    && isValidHostMessage(event.data)
}

export function postToHost(type, payload = {}) {
  if (typeof window === 'undefined' || window.parent === window) return false
  window.parent.postMessage(makeProtocolMessage(type, payload), '*')
  return true
}

export function acknowledgeSampleBatch(message) {
  if (!isValidSamplesBatch(message)) return false
  if (!isValidProtocolId(message.runId) || !isValidProtocolId(message.batchId)) return false
  if (!Number.isSafeInteger(message.firstSampleIndex) || !Number.isSafeInteger(message.lastSampleIndex)) return false
  return postToHost(DIGITAL_TWIN_MESSAGE.SAMPLES_ACK, {
    runId: message.runId,
    batchId: message.batchId,
    lastSampleIndex: message.lastSampleIndex,
  })
}
