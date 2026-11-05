import { MAX_SAMPLES_PER_BATCH } from './index.js'

const EWMA_ALPHA = 0.25
const MAX_TRANSFER_MS_PER_BYTE = 0.01
const MAX_BATCH_GROWTH_FACTOR = 2
const MIN_SAMPLE_COST_MS = 0.01
const DEFAULT_TARGET_RESPONSE_MS = 200
const DEFAULT_MAX_COMPUTE_MS = 50
const DEFAULT_PRESENTATION_RESERVE_MS = 50
const DEFAULT_SAFETY_MARGIN_MS = 20

function updateEwma(previous, current) {
  if (!Number.isFinite(current) || current < 0) return previous
  return previous === null ? current : previous * (1 - EWMA_ALPHA) + current * EWMA_ALPHA
}

function updateTransferCost(controller, transportMs, payloadBytes) {
  const previousBytes = controller.previousPayloadBytes
  const previousTransportMs = controller.previousTransportMs
  let transferMsPerByte = controller.transferMsPerByte

  if (Number.isFinite(previousBytes) && Number.isFinite(previousTransportMs)
    && payloadBytes - previousBytes >= 1024) {
    const measured = (transportMs - previousTransportMs) / (payloadBytes - previousBytes)
    if (Number.isFinite(measured) && measured >= 0 && measured <= MAX_TRANSFER_MS_PER_BYTE) {
      transferMsPerByte = transferMsPerByte === null
        ? measured
        : transferMsPerByte * (1 - EWMA_ALPHA) + measured * EWMA_ALPHA
    }
  }

  return {
    transferMsPerByte,
    previousPayloadBytes: payloadBytes,
    previousTransportMs: transportMs,
  }
}

export function createAdaptiveBatchController({
  startTime,
  stopTime,
  stepSize,
  maxSamples = MAX_SAMPLES_PER_BATCH,
  targetResponseMs = DEFAULT_TARGET_RESPONSE_MS,
  maxComputeMs = DEFAULT_MAX_COMPUTE_MS,
  presentationReserveMs = DEFAULT_PRESENTATION_RESERVE_MS,
  safetyMarginMs = DEFAULT_SAFETY_MARGIN_MS,
}) {
  if (![startTime, stepSize, maxSamples, targetResponseMs, maxComputeMs, presentationReserveMs, safetyMarginMs].every(Number.isFinite)
    || (stopTime !== undefined && stopTime !== null && (!Number.isFinite(stopTime) || stopTime <= startTime))
    || stepSize <= 0 || !Number.isInteger(maxSamples) || maxSamples < 1
    || targetResponseMs < 1 || maxComputeMs < 1 || maxComputeMs > 1000
    || presentationReserveMs < 0 || safetyMarginMs < 0) {
    throw new TypeError('A valid simulation interval and adaptive batch budget are required.')
  }

  return {
    maxSamples: Math.min(MAX_SAMPLES_PER_BATCH, Math.floor(maxSamples)),
    sampleCount: 1,
    targetResponseMs,
    maxComputeMs,
    presentationReserveMs,
    safetyMarginMs,
    responseEwmaMs: null,
    computeEwmaMs: null,
    computePerSampleEwmaMs: null,
    fixedLatencyEwmaMs: null,
    transferMsPerByte: null,
    bytesPerSampleEwma: null,
    consumerDelayEwmaMs: null,
    rateLimitWaitEwmaMs: null,
    interactionDelayEwmaMs: null,
    targetReachable: null,
    estimatedBatchResponseMs: null,
    batchConstraint: 'measurement',
    lastStopReason: null,
    singleStepOverBudget: false,
    previousPayloadBytes: null,
    previousTransportMs: null,
  }
}

export function getAdaptiveBatchSize(controller) {
  return Math.max(1, Math.min(controller.sampleCount, controller.maxSamples, MAX_SAMPLES_PER_BATCH))
}

export function getRealtimePacedBatchSize({
  currentTime,
  stopTime,
  stepSize,
  sampleEverySteps = 1,
  maxSamples,
  paceClock,
  now,
}) {
  if (![currentTime, stepSize, sampleEverySteps, maxSamples, now,
    paceClock?.simTime, paceClock?.wallTime].every(Number.isFinite)
    || (stopTime !== undefined && stopTime !== null && !Number.isFinite(stopTime))
    || stepSize <= 0 || sampleEverySteps < 1 || !Number.isInteger(sampleEverySteps)
    || maxSamples < 1 || maxSamples > MAX_SAMPLES_PER_BATCH || !Number.isInteger(maxSamples)) {
    throw new TypeError('A valid real-time batch clock and simulation interval are required.')
  }

  const elapsedRealTime = Math.max(0, now - paceClock.wallTime) / 1000
  const availableSimulationTime = paceClock.simTime + elapsedRealTime
  const sampleInterval = stepSize * sampleEverySteps
  const tolerance = Math.max(1e-9, Math.abs(sampleInterval) * 1e-7)
  let availableSamples = Math.floor((availableSimulationTime - currentTime + tolerance) / sampleInterval)
  if (Number.isFinite(stopTime) && stopTime - currentTime < sampleInterval - tolerance
    && availableSimulationTime >= stopTime - tolerance) {
    availableSamples = Math.max(availableSamples, 1)
  }

  return Math.min(maxSamples, Math.max(1, availableSamples))
}

export function getRealtimePaceWaitMs({
  currentTime,
  stopTime,
  stepSize,
  sampleEverySteps = 1,
  paceClock,
  now,
}) {
  if (![currentTime, stepSize, sampleEverySteps, now,
    paceClock?.simTime, paceClock?.wallTime].every(Number.isFinite)
    || (stopTime !== undefined && stopTime !== null && !Number.isFinite(stopTime))
    || stepSize <= 0 || sampleEverySteps < 1 || !Number.isInteger(sampleEverySteps)) {
    throw new TypeError('A valid real-time batch clock and simulation interval are required.')
  }

  const nextTime = Number.isFinite(stopTime)
    ? Math.min(stopTime, currentTime + stepSize * sampleEverySteps)
    : currentTime + stepSize * sampleEverySteps
  const dueAt = paceClock.wallTime + Math.max(0, nextTime - paceClock.simTime) * 1000
  return Math.max(0, dueAt - now)
}

export function observeAdaptiveBatch(controller, {
  responseMs,
  computeMs,
  sampleCount,
  payloadBytes,
  consumerDelayMs,
  rateLimitWaitMs,
  interactionDelayMs,
  stopReason,
}) {
  const validSampleCount = Number.isInteger(sampleCount) && sampleCount > 0 ? sampleCount : 1
  const responseEwmaMs = updateEwma(controller.responseEwmaMs, responseMs)
  const computeEwmaMs = updateEwma(controller.computeEwmaMs, computeMs)
  const perSampleCompute = Number.isFinite(computeMs) && computeMs >= 0
    ? computeMs / validSampleCount
    : null
  const computePerSampleEwmaMs = updateEwma(controller.computePerSampleEwmaMs, perSampleCompute)
  const safePayloadBytes = Number.isFinite(payloadBytes) && payloadBytes >= 0 ? payloadBytes : null
  const transportMs = Number.isFinite(responseMs) && responseMs >= 0
    ? Math.max(0, responseMs - (Number.isFinite(computeMs) ? computeMs : 0))
    : null
  const transfer = transportMs !== null && safePayloadBytes !== null
    ? updateTransferCost(controller, transportMs, safePayloadBytes)
    : {
      transferMsPerByte: controller.transferMsPerByte,
      previousPayloadBytes: controller.previousPayloadBytes,
      previousTransportMs: controller.previousTransportMs,
    }
  const fixedLatencySample = transportMs === null
    ? null
    : Math.max(0, transportMs - (transfer.transferMsPerByte || 0) * (safePayloadBytes || 0))
  const fixedLatencyEwmaMs = updateEwma(controller.fixedLatencyEwmaMs, fixedLatencySample)
  const bytesPerSample = safePayloadBytes === null ? null : safePayloadBytes / validSampleCount
  const bytesPerSampleEwma = updateEwma(controller.bytesPerSampleEwma, bytesPerSample)
  const consumerDelayEwmaMs = updateEwma(controller.consumerDelayEwmaMs, consumerDelayMs)
  const rateLimitWaitEwmaMs = updateEwma(controller.rateLimitWaitEwmaMs, rateLimitWaitMs)
  const interactionDelayEwmaMs = updateEwma(controller.interactionDelayEwmaMs, interactionDelayMs)
  const computeCostPerSample = computePerSampleEwmaMs ?? MIN_SAMPLE_COST_MS
  const transferCostPerSample = (transfer.transferMsPerByte || 0) * (bytesPerSampleEwma || 0)
  const totalMarginalSampleCost = Math.max(MIN_SAMPLE_COST_MS, computeCostPerSample + transferCostPerSample)
  const fixedLatencyMs = fixedLatencyEwmaMs ?? transportMs ?? 0
  const presentationAllowanceMs = consumerDelayEwmaMs ?? controller.presentationReserveMs
  const targetSampleBudgetMs = controller.targetResponseMs
    - fixedLatencyMs
    - presentationAllowanceMs
    - controller.safetyMarginMs
  const targetReachable = targetSampleBudgetMs >= totalMarginalSampleCost
  const fallbackSampleBudgetMs = Math.max(
    0,
    fixedLatencyMs - presentationAllowanceMs - controller.safetyMarginMs,
  )
  const latencyLimitedSamples = Math.max(1, Math.floor(
    (targetReachable ? targetSampleBudgetMs : fallbackSampleBudgetMs) / totalMarginalSampleCost,
  ))
  const computeLimitedSamples = computeCostPerSample > 0
    ? Math.floor(controller.maxComputeMs / computeCostPerSample)
    : controller.maxSamples
  const desiredSampleCount = Math.max(1, Math.min(
    controller.maxSamples,
    latencyLimitedSamples,
    Math.max(1, computeLimitedSamples),
  ))
  const nextSampleCount = desiredSampleCount > controller.sampleCount
    ? Math.min(desiredSampleCount, controller.sampleCount * MAX_BATCH_GROWTH_FACTOR)
    : desiredSampleCount
  const batchConstraint = !targetReachable
    ? 'latency-unreachable'
    : latencyLimitedSamples <= computeLimitedSamples && latencyLimitedSamples <= controller.maxSamples
      ? 'response-budget'
      : computeLimitedSamples <= controller.maxSamples ? 'compute-budget' : 'sample-limit'

  return {
    ...controller,
    ...transfer,
    sampleCount: nextSampleCount,
    responseEwmaMs,
    computeEwmaMs,
    computePerSampleEwmaMs,
    fixedLatencyEwmaMs,
    bytesPerSampleEwma,
    consumerDelayEwmaMs,
    rateLimitWaitEwmaMs,
    interactionDelayEwmaMs,
    targetReachable,
    estimatedBatchResponseMs: fixedLatencyMs
      + nextSampleCount * totalMarginalSampleCost
      + presentationAllowanceMs,
    batchConstraint,
    lastStopReason: stopReason || null,
    singleStepOverBudget: Number.isFinite(computeMs) && computeMs > controller.maxComputeMs && validSampleCount === 1,
  }
}
