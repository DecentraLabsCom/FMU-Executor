/** Resolve the displayed relative tolerance, preferring the explicit run option. */
export function resolveEffectiveTolerance(options, defaultTolerance) {
  const asPositiveFiniteNumber = (value) => {
    if (typeof value === 'boolean' || value === null || value === undefined || value === '') return null
    const parsed = Number(value)
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null
  }
  const requested = asPositiveFiniteNumber(options?.tolerance)
  if (requested !== null) {
    return { value: requested, source: 'run-option' }
  }
  const fallback = asPositiveFiniteNumber(defaultTolerance)
  if (fallback !== null) {
    return { value: fallback, source: 'default-experiment' }
  }
  return null
}
