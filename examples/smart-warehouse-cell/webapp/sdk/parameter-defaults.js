const NUMERIC_FMI_TYPES = new Set([
  'real', 'float32', 'float64', 'integer', 'enumeration',
  'int8', 'uint8', 'int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64',
])

function parseFiniteNumber(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function parseBoolean(value) {
  if (typeof value === 'boolean') return value
  if (typeof value !== 'string') return null
  const normalized = value.trim().toLowerCase()
  if (normalized === 'true' || normalized === '1') return true
  if (normalized === 'false' || normalized === '0') return false
  return null
}

function valuesMatchFmiStart(value, start, type) {
  if (Array.isArray(value) || Array.isArray(start)) {
    return Array.isArray(value) && Array.isArray(start)
      && value.length === start.length
      && value.every((item, index) => valuesMatchFmiStart(item, start[index], type))
  }

  const normalizedType = String(type || '').toLowerCase()
  if (NUMERIC_FMI_TYPES.has(normalizedType)
    || (typeof value === 'number' && typeof start === 'number')) {
    const numericValue = parseFiniteNumber(value)
    const numericStart = parseFiniteNumber(start)
    return numericValue !== null && numericStart !== null && numericValue === numericStart
  }

  if (normalizedType === 'boolean' || (typeof value === 'boolean' && typeof start === 'boolean')) {
    const booleanValue = parseBoolean(value)
    const booleanStart = parseBoolean(start)
    return booleanValue !== null && booleanStart !== null && booleanValue === booleanStart
  }

  return value === start
}

/** Remove initialization values that are identical to a declared FMI start. */
export function omitParametersMatchingFmiStart(parameters, modelVariables = []) {
  if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)
    || !Array.isArray(modelVariables) || modelVariables.length === 0) return parameters

  const variablesByName = new Map(modelVariables
    .filter((variable) => variable && typeof variable.name === 'string')
    .map((variable) => [variable.name, variable]))

  return Object.fromEntries(Object.entries(parameters).filter(([name, value]) => {
    const variable = variablesByName.get(name)
    if (!variable || variable.start === undefined || variable.start === null) return true
    return !valuesMatchFmiStart(value, variable.start, variable.type)
  }))
}
