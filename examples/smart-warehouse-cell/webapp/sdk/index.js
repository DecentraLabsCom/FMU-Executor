// Host integration v2 carries authorization and optional HMI controls only.
export const DIGITAL_TWIN_PROTOCOL_VERSION = 2
export const DIGITAL_TWIN_APP_ASSET_FILENAME = 'index.js'
export const MAX_RUN_PARAMETERS = 256
export const MAX_PARAMETER_NAME_LENGTH = 256
export const MAX_PARAMETER_STRING_LENGTH = 1024
export const MAX_OUTPUTS_PER_SAMPLE = 512
export const MAX_SAMPLES_PER_BATCH = 256
export const MAX_SAMPLE_BATCH_JSON_CHARS = 1_000_000
export const DIGITAL_TWIN_MESSAGE = Object.freeze({
  HOST_READY: 'decentralabs:digital-twin-host-ready',
  APP_READY: 'decentralabs:digital-twin-ready',
  APP_HELLO: 'decentralabs:digital-twin-connect',
  APP_ERROR: 'decentralabs:digital-twin-error',
  CONNECTION_REQUEST: 'decentralabs:fmu-connection-request',
  CONNECTION_READY: 'decentralabs:fmu-connection-ready',
  CONNECTION_ERROR: 'decentralabs:fmu-connection-error',
  EXECUTION_STATE: 'decentralabs:execution-state',
  EXECUTION_CANCEL: 'decentralabs:execution-cancel',
  PLAYBACK_CONTROL: 'decentralabs:fmi-playback-control',
  MODEL_CHANGE: 'decentralabs:digital-twin-model-change',
})
export const DIGITAL_TWIN_CAPABILITY = Object.freeze({ PLAYBACK_RATE: 'playback-rate', EXECUTION_CONTROL: 'execution-control' })
export const APP_TO_HOST_MESSAGE_TYPES = Object.freeze([
  DIGITAL_TWIN_MESSAGE.APP_HELLO,
  DIGITAL_TWIN_MESSAGE.APP_READY, DIGITAL_TWIN_MESSAGE.APP_ERROR, DIGITAL_TWIN_MESSAGE.CONNECTION_REQUEST,
  DIGITAL_TWIN_MESSAGE.EXECUTION_STATE, DIGITAL_TWIN_MESSAGE.MODEL_CHANGE,
])
export const HOST_TO_APP_MESSAGE_TYPES = Object.freeze([
  DIGITAL_TWIN_MESSAGE.HOST_READY, DIGITAL_TWIN_MESSAGE.CONNECTION_READY, DIGITAL_TWIN_MESSAGE.CONNECTION_ERROR,
  DIGITAL_TWIN_MESSAGE.EXECUTION_CANCEL, DIGITAL_TWIN_MESSAGE.PLAYBACK_CONTROL,
])
export const isRecord = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const keys = (v, allowed) => Object.keys(v).every((key) => allowed.includes(key))
const text = (v, max = 128) => typeof v === 'string' && v.length > 0 && v.length <= max
  && Array.from(v).every((character) => character.codePointAt(0) > 31 && character.codePointAt(0) !== 127)
export const isValidProtocolId = (v) => text(v) && /^[a-zA-Z0-9][a-zA-Z0-9._:-]*$/.test(v)
export const isProtocolEnvelope = (v) => isRecord(v) && v.version === DIGITAL_TWIN_PROTOCOL_VERSION && typeof v.type === 'string'
export const isFiniteScalar = (v) => typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v)) || (typeof v === 'string' && v.length <= 1024)
export const isValidOptions = (v) => isRecord(v) && Object.entries(v).every(([key, value]) => ['startTime', 'stopTime', 'stepSize'].includes(key) && typeof value === 'number' && Number.isFinite(value))
export const isValidParameters = (v) => isRecord(v) && Object.keys(v).length <= MAX_RUN_PARAMETERS && Object.entries(v).every(([key, value]) => text(key, 256) && isFiniteScalar(value))
export const isValidInputSchedule = (v) => Array.isArray(v) && v.length <= 128 && v.every((event) => isRecord(event) && keys(event, ['time', 'values']) && Number.isFinite(event.time) && isValidParameters(event.values))
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
  return isRecord(v) && keys(v, ['time', 'outputs']) && Number.isFinite(v.time) && isRecord(v.outputs)
    && Object.keys(v.outputs).length <= MAX_OUTPUTS_PER_SAMPLE
    && Object.entries(v.outputs).every(([key, value]) => text(key, 256) && (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))))
}
export function isValidAppReadyMessage(v) {
  return isProtocolEnvelope(v) && v.type === DIGITAL_TWIN_MESSAGE.APP_READY
    && keys(v, ['type', 'version', 'capabilities', 'defaultPlaybackRate', 'playbackControlPosition'])
    && (v.capabilities === undefined || (Array.isArray(v.capabilities) && v.capabilities.length <= 32 && new Set(v.capabilities).size === v.capabilities.length && v.capabilities.every((c) => text(c))))
    && (v.defaultPlaybackRate === undefined || (Number.isFinite(v.defaultPlaybackRate) && v.defaultPlaybackRate > 0 && v.defaultPlaybackRate <= 100000))
    && (v.playbackControlPosition === undefined || ['top-left', 'top-right', 'bottom-left', 'bottom-right'].includes(v.playbackControlPosition))
}
export const isValidAppErrorMessage = (v) => isProtocolEnvelope(v) && v.type === DIGITAL_TWIN_MESSAGE.APP_ERROR && keys(v, ['type', 'version', 'code']) && (v.code === undefined || text(v.code))
export function isValidAppMessage(v) {
  if (!isProtocolEnvelope(v)) return false
  switch (v.type) {
    case DIGITAL_TWIN_MESSAGE.APP_HELLO: return keys(v, ['type', 'version'])
    case DIGITAL_TWIN_MESSAGE.APP_READY: return isValidAppReadyMessage(v)
    case DIGITAL_TWIN_MESSAGE.APP_ERROR: return isValidAppErrorMessage(v)
    case DIGITAL_TWIN_MESSAGE.CONNECTION_REQUEST:
      return keys(v, ['type', 'version', 'requestId', 'options']) && isValidProtocolId(v.requestId) && (v.options === undefined || isValidOptions(v.options))
    case DIGITAL_TWIN_MESSAGE.EXECUTION_STATE:
      return keys(v, ['type', 'version', 'runId', 'state', 'options']) && isValidProtocolId(v.runId)
        && ['idle', 'running', 'completed', 'cancelled', 'error'].includes(v.state) && (v.options === undefined || isValidOptions(v.options))
    case DIGITAL_TWIN_MESSAGE.MODEL_CHANGE:
      return keys(v, ['type', 'version', 'modelId']) && text(v.modelId)
    default: return false
  }
}
export function isValidHostMessage(v) {
  if (!isProtocolEnvelope(v)) return false
  switch (v.type) {
    case DIGITAL_TWIN_MESSAGE.HOST_READY:
      return keys(v, ['type', 'version', 'extensions']) && (v.extensions === undefined || (isRecord(v.extensions) && Object.keys(v.extensions).length <= 32))
    case DIGITAL_TWIN_MESSAGE.CONNECTION_READY:
      return keys(v, ['type', 'version', 'requestId', 'connection', 'inputSchedule']) && isValidProtocolId(v.requestId) && isValidConnectionConfig(v.connection) && (v.inputSchedule === undefined || isValidInputSchedule(v.inputSchedule))
    case DIGITAL_TWIN_MESSAGE.CONNECTION_ERROR:
      return keys(v, ['type', 'version', 'requestId', 'code']) && isValidProtocolId(v.requestId) && text(v.code)
    case DIGITAL_TWIN_MESSAGE.EXECUTION_CANCEL:
      return keys(v, ['type', 'version', 'runId']) && isValidProtocolId(v.runId)
    case DIGITAL_TWIN_MESSAGE.PLAYBACK_CONTROL:
      return keys(v, ['type', 'version', 'rate', 'rateUnit']) && Number.isFinite(v.rate) && v.rate > 0 && v.rate <= 100000 && v.rateUnit === 'model-seconds-per-real-second'
    default: return false
  }
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
