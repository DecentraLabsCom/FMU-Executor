import { createProtocolId, isValidConnectionConfig, MAX_SAMPLE_BATCH_JSON_CHARS } from './index.js'
import { getInputUpdateQuota, getInputUpdateRetryDelayMs } from './input-update-quota.js'
import { omitParametersMatchingFmiStart } from './parameter-defaults.js'

const failure = (message, code = 'FMU_CONNECTION_FAILED') => Object.assign(new Error(message), { code })
const CHECKPOINT_FIELDS = ['format', 'checkpointId', 'scope', 'createdAt', 'expiresAt', 'signature']

export function isValidFmuStateCheckpoint(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === CHECKPOINT_FIELDS.length
    && Object.keys(value).every((key) => CHECKPOINT_FIELDS.includes(key))
    && value.format === 'decentralabs-fmi-state-v1'
    && typeof value.checkpointId === 'string' && /^[a-f\d]{32}$/.test(value.checkpointId)
    && typeof value.scope === 'string' && /^[a-f\d]{64}$/.test(value.scope)
    && Number.isFinite(value.createdAt) && Number.isFinite(value.expiresAt)
    && value.expiresAt > value.createdAt && value.expiresAt > Date.now() / 1000
    && typeof value.signature === 'string' && /^[a-f\d]{64}$/.test(value.signature)
}

export function isValidFmuStateCheckpointBundle(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length !== 3 || !['version', 'simTime', 'checkpoint'].every((key) => key in value)
    || value.version !== 1 || !Number.isFinite(value.simTime)
    || !isValidFmuStateCheckpoint(value.checkpoint)) return false
  try { return new TextEncoder().encode(JSON.stringify(value)).length <= 4096 } catch { return false }
}

function waitForRetry(delayMs, signal) {
  signal?.throwIfAborted()
  return new Promise((resolve, reject) => {
    let timeout
    const onAbort = () => {
      clearTimeout(timeout)
      signal.removeEventListener('abort', onAbort)
      reject(signal.reason || new DOMException('FMU request cancelled.', 'AbortError'))
    }
    timeout = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, delayMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    if (signal?.aborted) onAbort()
  })
}

/** Connect directly to the provider's existing proxy.fmu WebSocket service.
 * The one-use ticket is sent only in session.create, never in a URL or storage.
 * This client is independent of the Marketplace window and its UI protocol.
 */
export async function connectFmuSession(connection, { signal, timeoutMs = 120_000 } = {}) {
  if (!isValidConnectionConfig(connection) || connection.ticketExpiresAt <= Date.now() / 1000) {
    throw failure('The FMU connection grant is invalid or expired.', 'INVALID_CONNECTION_GRANT')
  }
  signal?.throwIfAborted()
  const socket = new WebSocket(connection.gatewayWsUrl)
  const pending = new Map()
  let sessionId
  let capabilities = {}
  let modelVariablesForStart = null
  let closed = false
  let openResolve
  let openReject
  const opened = new Promise((resolve, reject) => { openResolve = resolve; openReject = reject })
  const openTimeout = setTimeout(() => close(failure('The FMU connection timed out.')), 30_000)
  const abort = () => close(signal.reason || new DOMException('Cancelled', 'AbortError'))

  function close(error = failure('The FMU session closed.')) {
    if (closed) return
    closed = true
    clearTimeout(openTimeout)
    signal?.removeEventListener('abort', abort)
    openReject(error)
    for (const item of pending.values()) { clearTimeout(item.timeout); item.reject(error) }
    pending.clear()
    socket.close()
  }
  signal?.addEventListener('abort', abort, { once: true })
  socket.addEventListener('open', () => { clearTimeout(openTimeout); openResolve() }, { once: true })
  socket.addEventListener('error', () => close(failure('The provider FMU service could not be reached.')))
  socket.addEventListener('close', () => close(failure('The provider FMU session disconnected.', 'FMU_DISCONNECTED')))
  socket.addEventListener('message', (event) => {
    try {
      if (typeof event.data !== 'string' || new TextEncoder().encode(event.data).length > MAX_SAMPLE_BATCH_JSON_CHARS) {
        throw failure('The FMU response exceeds the frame limit.', 'INVALID_FMU_RESPONSE')
      }
      const response = JSON.parse(event.data)
      if (!response || typeof response !== 'object' || typeof response.type !== 'string') throw failure('Invalid FMU response.')
      if (response.type === 'session.closed') { close(failure('The FMU session ended.', response.reason)); return }
      const item = pending.get(response.requestId)
      if (!item) return
      if (sessionId && response.sessionId && response.sessionId !== sessionId) throw failure('The FMU response belongs to another session.')
      pending.delete(response.requestId)
      clearTimeout(item.timeout)
      if (response.type === 'error') {
        item.reject(Object.assign(failure(response.message || 'The FMU operation failed.', response.code), {
          retryAfterMs: response.retryAfterMs,
          retryable: response.retryable,
          remaining: response.remaining,
          maxBatchSize: response.maxBatchSize,
        }))
      } else if (response.type !== item.expected) {
        item.reject(failure('The FMU returned an unexpected response.', 'INVALID_FMU_RESPONSE'))
      } else item.resolve(response)
    } catch (error) { close(error) }
  })

  async function request(type, expected, payload = {}) {
    const hasInitializationParameters = payload.parameters
      && typeof payload.parameters === 'object' && !Array.isArray(payload.parameters)
      && Object.keys(payload.parameters).length > 0
    if (type === 'sim.initialize' && hasInitializationParameters && modelVariablesForStart === null) {
      try {
        await request('model.describe', 'model.description', { includeVariables: true })
      } catch (error) {
        if (closed || signal?.aborted) throw error
      }
    }

    let requestPayload = payload
    if (type === 'model.describe') {
      requestPayload = { ...payload, includeVariables: true }
    } else if (type === 'sim.initialize' && payload.parameters
      && typeof payload.parameters === 'object' && !Array.isArray(payload.parameters)) {
      requestPayload = {
        ...payload,
        parameters: omitParametersMatchingFmiStart(payload.parameters, modelVariablesForStart || []),
      }
    }

    const hasInputUpdates = type === 'sim.setInputs'
      ? Boolean(payload.values && Object.keys(payload.values).length)
      : type === 'sim.advance' && (
        (Array.isArray(payload.inputSchedule) && payload.inputSchedule.length > 0)
        || Boolean(payload.inputValues && Object.keys(payload.inputValues).length)
      )

    while (true) {
      if (closed) throw failure('The FMU session is closed.')
      signal?.throwIfAborted()
      const requestId = createProtocolId('rpc')
      try {
        const response = await new Promise((resolve, reject) => {
          const timeout = setTimeout(() => {
            pending.delete(requestId)
            reject(failure('The FMU operation timed out.', 'FMU_REQUEST_TIMEOUT'))
            close()
          }, timeoutMs)
          pending.set(requestId, { resolve, reject, timeout, expected })
          try { socket.send(JSON.stringify({ ...requestPayload, type, requestId, ...(sessionId ? { sessionId } : {}) })) }
          catch (error) { close(error) }
        })
        if (type === 'model.describe') {
          modelVariablesForStart = Array.isArray(response.modelVariables) ? response.modelVariables : []
        }
        return response
      } catch (error) {
        if (!hasInputUpdates) throw error
        const retryAfterMs = getInputUpdateRetryDelayMs(error, capabilities)
        if (retryAfterMs === null) throw error
        // The provider rejects over-quota writes before applying their inputs or advancing the FMU.
        await waitForRetry(retryAfterMs, signal)
      }
    }
  }

  try {
    await opened
    const created = await request('session.create', 'session.created', {
      labId: connection.labId, reservationKey: connection.reservationKey, sessionTicket: connection.sessionTicket,
    })
    if (typeof created.sessionId !== 'string' || !created.sessionId) throw failure('Missing FMU session identifier.')
    getInputUpdateQuota(created.capabilities || {})
    capabilities = created.capabilities || {}
    sessionId = created.sessionId
    function requireFmuStateSupport() {
      if (capabilities.fmuState?.getAndSet !== true || capabilities.fmuState?.serialize !== true) {
        throw failure('The provider FMU runtime does not support serialized FMU states.', 'UNSUPPORTED_FMU_STATE')
      }
    }

    async function captureState() {
      requireFmuStateSupport()
      const response = await request('sim.captureFMUState', 'sim.fmuState')
      const bundle = { version: 1, simTime: Number(response.simTime), checkpoint: response.checkpoint }
      if (!isValidFmuStateCheckpointBundle(bundle)) {
        throw failure('The provider returned an invalid FMU state checkpoint.', 'INVALID_FMU_STATE_CHECKPOINT')
      }
      return bundle
    }

    async function restoreState(value) {
      requireFmuStateSupport()
      const checkpoint = isValidFmuStateCheckpointBundle(value) ? value.checkpoint : value
      if (!isValidFmuStateCheckpoint(checkpoint)) {
        throw failure('The FMU state checkpoint is invalid.', 'INVALID_FMU_STATE_CHECKPOINT')
      }
      return request('sim.restoreFMUState', 'sim.state', { checkpoint })
    }

    return {
      sessionId,
      capabilities,
      request,
      captureState,
      restoreState,
      close() {
        if (!closed && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'session.terminate', requestId: createProtocolId('close'), sessionId }))
        }
        close()
      },
    }
  } catch (error) { close(error); throw error }
}
