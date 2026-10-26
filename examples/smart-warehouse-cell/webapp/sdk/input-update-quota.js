export function getInputUpdateQuota(capabilities) {
  const quota = capabilities?.inputUpdateQuota
  const positiveIntegerLimits = [
    quota?.maxUpdatesPerWindow,
    quota?.maxUpdateBytes,
    quota?.maxUpdatesPerDay,
    quota?.maxUpdateBytesPerDay,
    quota?.maxScheduledEventsPerAdvance,
    quota?.maxScheduleBytesPerAdvance,
  ]
  const windowMs = quota?.windowMs
  if (positiveIntegerLimits.some((limit) => !Number.isSafeInteger(limit) || limit < 1)
    || !Number.isFinite(windowMs) || windowMs < 1 || windowMs > 3_600_000) {
    throw Object.assign(
      new Error('The FMU runtime did not advertise a valid input update quota.'),
      { code: 'UNSUPPORTED_INPUT_UPDATE_QUOTA' },
    )
  }
  return {
    maxUpdatesPerWindow: quota.maxUpdatesPerWindow,
    windowMs,
    maxUpdateBytes: quota.maxUpdateBytes,
    maxUpdatesPerDay: quota.maxUpdatesPerDay,
    maxUpdateBytesPerDay: quota.maxUpdateBytesPerDay,
    maxScheduledEventsPerAdvance: quota.maxScheduledEventsPerAdvance,
    maxScheduleBytesPerAdvance: quota.maxScheduleBytesPerAdvance,
  }
}

export function getInputUpdateIntervalMs(capabilities) {
  const { maxUpdatesPerWindow, windowMs } = getInputUpdateQuota(capabilities)
  return windowMs / maxUpdatesPerWindow
}

export function getInputUpdateRetryDelayMs(error, capabilities) {
  if (error?.code !== 'INPUT_UPDATE_RATE_LIMIT') return null
  const { windowMs } = getInputUpdateQuota(capabilities)
  const retryAfterMs = error.retryAfterMs
  if (!Number.isFinite(retryAfterMs) || retryAfterMs < 1 || retryAfterMs > windowMs + 1000) return null
  return retryAfterMs
}
