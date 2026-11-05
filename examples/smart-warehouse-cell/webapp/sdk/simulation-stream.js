/** Read and decode the Marketplace/Gateway NDJSON simulation stream. */
export async function readNdjsonStream(response, onEvent, { signal } = {}) {
  if (!response?.body || typeof response.body.getReader !== 'function') {
    throw new Error('The simulation response did not include a readable stream.')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const cancelOnAbort = () => { void reader.cancel(signal?.reason).catch(() => {}) }
  signal?.addEventListener('abort', cancelOnAbort, { once: true })

  const emitLine = async (line) => {
    if (!line.trim()) return
    if (signal?.aborted) throw signal.reason || new DOMException('The operation was aborted.', 'AbortError')
    await onEvent(JSON.parse(line))
  }

  try {
    for (;;) {
      if (signal?.aborted) throw signal.reason || new DOMException('The operation was aborted.', 'AbortError')
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      for (const line of lines) await emitLine(line)
    }
    buffer += decoder.decode()
    await emitLine(buffer)
  } catch (error) {
    if (signal?.aborted) {
      try { await reader.cancel(signal.reason) } catch { /* The response may already be closed. */ }
    }
    throw error
  } finally {
    signal?.removeEventListener('abort', cancelOnAbort)
    reader.releaseLock?.()
  }
}

/** Normalize the two stream payload formats currently emitted by Gateway. */
export function normalizeSimulationStreamEvent(event, sequenceState = { nextSequence: 0 }) {
  if (!event || typeof event !== 'object' || Array.isArray(event)) return { kind: 'ignored' }
  if (event.type === 'started') return { kind: 'started', event }
  if (event.type === 'progress') return { kind: 'progress', event }

  if (event.type === 'sim.step') {
    if (!Number.isFinite(event.time) || !event.outputs || typeof event.outputs !== 'object' || Array.isArray(event.outputs)) {
      return { kind: 'ignored' }
    }
    const sequence = Number.isSafeInteger(event.sequence) && event.sequence > 0
      ? event.sequence
      : ++sequenceState.nextSequence
    sequenceState.nextSequence = Math.max(sequenceState.nextSequence, sequence)
    return {
      kind: 'samples',
      samples: [{
        time: event.time,
        outputs: event.outputs,
        sequence,
        ...Object.fromEntries(['eventOccurred', 'eventHandlingNeeded', 'earlyReturn', 'terminateSimulation', 'finished', 'finishReason']
          .filter((key) => event[key] !== undefined)
          .map((key) => [key, event[key]])),
      }],
      event,
    }
  }

  if (event.type === 'data') {
    const times = Array.isArray(event.time) ? event.time : [event.time]
    if (!times.length || !event.outputs || typeof event.outputs !== 'object' || Array.isArray(event.outputs)) {
      return { kind: 'ignored' }
    }
    const samples = []
    for (let index = 0; index < times.length; index += 1) {
      if (!Number.isFinite(times[index])) continue
      const sequence = ++sequenceState.nextSequence
      const outputs = Object.fromEntries(Object.entries(event.outputs).flatMap(([name, values]) => {
        const value = Array.isArray(values) ? values[index] : values
        return value === undefined ? [] : [[name, value]]
      }))
      samples.push({ time: times[index], outputs, sequence })
    }
    return samples.length ? { kind: 'samples', samples, event } : { kind: 'ignored' }
  }

  if (event.type === 'completed' || event.type === 'sim.done') {
    const time = Number.isFinite(event.simulationTime) ? event.simulationTime : event.time
    return {
      kind: 'completed',
      time: Number.isFinite(time) ? time : undefined,
      finishReason: typeof event.finishReason === 'string' ? event.finishReason : undefined,
      checkpoint: event.checkpoint,
      checkpointTime: Number.isFinite(event.checkpointTime) ? event.checkpointTime : undefined,
      metadata: event,
    }
  }

  if (event.type === 'error') {
    return {
      kind: 'error',
      code: typeof event.code === 'string' ? event.code : 'SIMULATION_FAILED',
      message: typeof event.detail === 'string' ? event.detail
        : typeof event.message === 'string' ? event.message : 'The simulation failed.',
    }
  }
  return { kind: 'ignored' }
}

export function selectSimulationOutputs(outputs, outputVariables) {
  if (!Array.isArray(outputVariables)) return outputs
  const selected = new Set(outputVariables)
  return Object.fromEntries(Object.entries(outputs || {}).filter(([name]) => selected.has(name)))
}
