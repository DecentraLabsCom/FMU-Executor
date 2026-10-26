import { MAX_SAMPLES_PER_BATCH } from './index.js'

const MAX_BATCH_HORIZON_FRACTION = 0.02
const EWMA_ALPHA = 0.25
const HIGH_RESPONSE_MS = 200
const LOW_RESPONSE_MS = 80
const HIGH_CONSUMER_DELAY_MS = 100
const LOW_CONSUMER_DELAY_MS = 50
const STABLE_BATCHES_TO_GROW = 3

function updateEwma(previous, current) {
  if (!Number.isFinite(current)) return previous
  return previous === null ? current : previous * (1 - EWMA_ALPHA) + current * EWMA_ALPHA
}

export function createAdaptiveBatchController({ startTime, stopTime, stepSize, maxSamples = MAX_SAMPLES_PER_BATCH }) {
  if (![startTime, stopTime, stepSize, maxSamples].every(Number.isFinite)
    || stopTime <= startTime || stepSize <= 0 || !Number.isInteger(maxSamples) || maxSamples < 1) {
    throw new TypeError('A valid simulation horizon and communication step are required.')
  }

  const configuredStepCount = Math.max(1, Math.ceil((stopTime - startTime) / stepSize))
  const modelHorizonSampleLimit = Math.max(1, Math.floor(configuredStepCount * MAX_BATCH_HORIZON_FRACTION))

  return {
    maxSamples: Math.min(MAX_SAMPLES_PER_BATCH, Math.floor(maxSamples), modelHorizonSampleLimit),
    sampleCount: 1,
    stableBatches: 0,
    responseEwmaMs: null,
    consumerDelayEwmaMs: null,
  }
}

export function getAdaptiveBatchSize(controller) {
  return Math.max(1, Math.min(controller.sampleCount, controller.maxSamples, MAX_SAMPLES_PER_BATCH))
}

export function observeAdaptiveBatch(controller, { responseMs, consumerDelayMs }) {
  const hasResponseMeasurement = Number.isFinite(responseMs)
  const hasConsumerDelayMeasurement = Number.isFinite(consumerDelayMs)
  const responseEwmaMs = updateEwma(controller.responseEwmaMs, responseMs)
  const consumerDelayEwmaMs = updateEwma(controller.consumerDelayEwmaMs, consumerDelayMs)
  const overloaded = responseEwmaMs > HIGH_RESPONSE_MS
    || (hasConsumerDelayMeasurement && consumerDelayEwmaMs > HIGH_CONSUMER_DELAY_MS)

  let sampleCount = controller.sampleCount
  let stableBatches = controller.stableBatches
  if (overloaded) {
    sampleCount = Math.max(1, Math.floor(sampleCount / 2))
    stableBatches = 0
  } else if (hasResponseMeasurement && hasConsumerDelayMeasurement
    && responseEwmaMs < LOW_RESPONSE_MS && consumerDelayEwmaMs < LOW_CONSUMER_DELAY_MS) {
    stableBatches += 1
    if (stableBatches >= STABLE_BATCHES_TO_GROW) {
      sampleCount = Math.min(controller.maxSamples, Math.max(sampleCount + 1, Math.ceil(sampleCount * 1.25)))
      stableBatches = 0
    }
  } else {
    stableBatches = 0
  }

  return {
    ...controller,
    sampleCount,
    stableBatches,
    responseEwmaMs,
    consumerDelayEwmaMs,
  }
}
