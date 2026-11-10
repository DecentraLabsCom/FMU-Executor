const MAX_MODEL_VARIABLES = 4096
const MAX_DEPENDENCY_OUTPUTS = 4096
const MAX_DEPENDENCY_NAMES = 8192

/** Project reservation-authorized model metadata to a small host-to-app payload. */
export function createFmiDependencyMetadata(modelVariables, modelStructure) {
  const variables = Array.isArray(modelVariables) ? modelVariables : []
  const safeVariables = variables.slice(0, MAX_MODEL_VARIABLES).flatMap((variable) => {
    if (!variable || typeof variable !== 'object' || Array.isArray(variable)
      || typeof variable.name !== 'string' || !variable.name || variable.name.length > 512
      || !['input', 'output'].includes(variable.causality)) return []
    const safe = { name: variable.name, causality: variable.causality }
    for (const [field, limit] of Object.entries({ type: 64, variability: 32, description: 512, quantity: 256, unit: 128, displayUnit: 128 })) {
      if (typeof variable[field] === 'string' && variable[field].length) safe[field] = variable[field].slice(0, limit)
    }
    return [safe]
  })
  const inputNames = new Set(safeVariables.filter((variable) => variable.causality === 'input').map((variable) => variable.name))
  const outputNames = new Set(safeVariables.filter((variable) => variable.causality === 'output').map((variable) => variable.name))
  let dependencyCount = 0
  const outputs = []
  for (const entry of Array.isArray(modelStructure?.outputs) ? modelStructure.outputs : []) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)
      || typeof entry.name !== 'string' || entry.name.length > 512 || !outputNames.has(entry.name)
      || !Array.isArray(entry.dependencyNames)) continue
    const dependencyNames = [...new Set(entry.dependencyNames.filter((name) => inputNames.has(name)))].slice(0, 256)
    if (!dependencyNames.length) continue
    const allowed = dependencyNames.slice(0, Math.max(0, MAX_DEPENDENCY_NAMES - dependencyCount))
    if (!allowed.length) break
    dependencyCount += allowed.length
    outputs.push({ name: entry.name, dependencyNames: allowed })
    if (outputs.length >= MAX_DEPENDENCY_OUTPUTS || dependencyCount >= MAX_DEPENDENCY_NAMES) break
  }
  return { modelVariables: safeVariables, modelStructure: { outputs } }
}

/**
 * Return FMI-declared output dependencies that match the currently selected
 * inputs. This is a recommendation helper; callers keep all variables visible.
 */
export function getDependentOutputRecommendations(modelStructure, selectedInputNames) {
  const selectedList = selectedInputNames instanceof Set
    ? [...selectedInputNames]
    : Array.isArray(selectedInputNames)
      ? selectedInputNames
      : selectedInputNames && typeof selectedInputNames[Symbol.iterator] === 'function'
        ? [...selectedInputNames]
        : []
  const selectedInputs = new Set(selectedList.filter((name) => typeof name === 'string' && name.length > 0))
  if (!selectedInputs.size || !Array.isArray(modelStructure?.outputs)) return []

  const seenOutputs = new Set()
  return modelStructure.outputs.flatMap((entry) => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)
      || typeof entry.name !== 'string' || !entry.name || seenOutputs.has(entry.name)
      || !Array.isArray(entry.dependencyNames)) return []
    const dependencyNames = [...new Set(entry.dependencyNames.filter((name) => (
      typeof name === 'string' && selectedInputs.has(name)
    )))]
    if (!dependencyNames.length) return []
    seenOutputs.add(entry.name)
    return [{ outputName: entry.name, dependencyNames }]
  })
}
