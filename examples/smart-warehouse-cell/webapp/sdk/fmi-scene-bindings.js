/**
 * FMI bindings carried in glTF node `extras`. Whole-vector targets follow
 * glTF animation channel paths (`translation`, quaternion `rotation`, `scale`,
 * and morph-target `weights`); scalar component targets are viewer controls.
 */
export const FMI_SCENE_BINDING_TARGETS = Object.freeze([
  'position.x', 'position.y', 'position.z',
  'translation.x', 'translation.y', 'translation.z',
  'translation', 'rotation', 'scale', 'weights',
  'rotation.x', 'rotation.y', 'rotation.z',
  'scale.x', 'scale.y', 'scale.z',
  'visible',
  'material.color',
])
const FMI_SCENE_BINDING_TARGET_SET = new Set(FMI_SCENE_BINDING_TARGETS)

const TRANSFORM_COMPONENT_PATTERN = /^(?:position|translation|rotation|scale)\.([xyz])$/
const VECTOR_TARGET_LENGTHS = new Map([
  ['translation', 3],
  ['rotation', 4],
  ['scale', 3],
])
const NUMERIC_INPUT_TYPES = new Set([
  'real', 'integer', 'float32', 'float64',
  'int8', 'int16', 'int32', 'int64', 'uint8', 'uint16', 'uint32', 'uint64',
])
const INTEGER_INPUT_TYPES = new Set([
  'integer', 'int8', 'int16', 'int32', 'int64', 'uint8', 'uint16', 'uint32', 'uint64',
])
const MAX_SCENE_COORDINATE = 100_000

export function getFmiInitialValue(variable) {
  if (!variable || variable.start === undefined || variable.start === null) return undefined

  if (variable.type === 'Boolean' || typeof variable.start === 'boolean') {
    return variable.start === true || variable.start === 'true' || variable.start === 1 || variable.start === '1'
  }

  const numericStart = Number(variable.start)
  return Number.isFinite(numericStart) ? numericStart : undefined
}

export function isFmiSceneBindingVisible(value, binding = {}) {
  if (Object.prototype.hasOwnProperty.call(binding, 'visibleWhenGreaterThanOrEqual')) {
    if (value === undefined || value === null) return false
    const threshold = Number(binding.visibleWhenGreaterThanOrEqual)
    const numericValue = Number(value)
    return Number.isFinite(threshold) && Number.isFinite(numericValue) && numericValue >= threshold
  }

  return Boolean(value)
}

function numericValue(value) {
  if (typeof value === 'boolean') return value ? 1 : 0
  if (typeof value !== 'number' && typeof value !== 'string') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function getBindingNumber(value, fallback) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function isFiniteBindingNumber(value) {
  return typeof value === 'number' && Number.isFinite(value)
}

function isBindingColor(value) {
  if (typeof value === 'string') return value.trim().length > 0
  return typeof value === 'number'
    && Number.isInteger(value)
    && value >= 0
    && value <= 0xffffff
}

function hasValidBindingFields(binding) {
  for (const field of ['scale', 'offset', 'visibleWhenGreaterThanOrEqual']) {
    if (Object.prototype.hasOwnProperty.call(binding, field) && !isFiniteBindingNumber(binding[field])) {
      return false
    }
  }
  for (const field of ['trueColor', 'falseColor']) {
    if (Object.prototype.hasOwnProperty.call(binding, field) && !isBindingColor(binding[field])) {
      return false
    }
  }
  if (Object.prototype.hasOwnProperty.call(binding, 'valueMap')) {
    if (!binding.valueMap || typeof binding.valueMap !== 'object' || Array.isArray(binding.valueMap)) return false
    const entries = Object.entries(binding.valueMap)
    if (entries.length === 0 || entries.length > 128
      || entries.some(([key, value]) => key.length > 128
        || !(typeof value === 'boolean' || isBindingColor(value)))) return false
  }
  if (Object.prototype.hasOwnProperty.call(binding, 'index')
    && (!Number.isInteger(binding.index) || binding.index < 0)) return false
  if (Object.prototype.hasOwnProperty.call(binding, 'indices')
    && (!Array.isArray(binding.indices)
      || binding.indices.length > 64
      || binding.indices.some((index) => !Number.isInteger(index) || index < 0))) return false
  return true
}

function getVariableDimensions(variable) {
  if (Array.isArray(variable?.dimensions) && variable.dimensions.length) return variable.dimensions
  return Array.isArray(variable?.start) ? [{ start: variable.start.length }] : []
}

function getDimensionVariable(dimension, modelVariables) {
  return modelVariables.find((candidate) => (
    (dimension?.variableName && candidate?.name === dimension.variableName)
    || (dimension?.valueReference != null && candidate?.valueReference === dimension.valueReference)
  ))
}

function getDimensionLimit(dimension, modelVariables) {
  if (Number.isInteger(dimension?.start) && dimension.start >= 0) return dimension.start
  const variable = getDimensionVariable(dimension, modelVariables)
  const maximum = Number(variable?.max)
  // A structural parameter's start value is only the default shape, not an
  // upper bound. Its FMI max attribute is the only safe static limit.
  return variable?.max != null && Number.isSafeInteger(maximum) && maximum >= 0 ? maximum : null
}

function isValidArraySelection(binding, variable, modelVariables, allowWholeArray) {
  const dimensions = getVariableDimensions(variable)
  if (!Object.prototype.hasOwnProperty.call(binding, 'indices')) return !dimensions.length || allowWholeArray
  if (allowWholeArray || !dimensions.length || binding.indices.length !== dimensions.length) return false
  return binding.indices.every((index, axis) => {
    const limit = getDimensionLimit(dimensions[axis], modelVariables)
    return limit === null || index < limit
  })
}

function isArrayLike(value) {
  return Array.isArray(value) || ArrayBuffer.isView(value)
}

function toArray(value) {
  return Array.isArray(value) ? value : (ArrayBuffer.isView(value) ? Array.from(value) : null)
}

function getCurrentDimensionSizes(variable, modelVariables, runtimeValues, flatLength = null) {
  const dimensions = getVariableDimensions(variable)
  if (!dimensions.length) return []
  const sizes = dimensions.map((dimension) => {
    if (Number.isInteger(dimension?.start) && dimension.start >= 0) return dimension.start
    const dimensionVariable = getDimensionVariable(dimension, modelVariables)
    const activeValue = dimensionVariable?.name && Object.prototype.hasOwnProperty.call(runtimeValues || {}, dimensionVariable.name)
      ? runtimeValues[dimensionVariable.name]
      : dimensionVariable?.start
    const candidate = numericValue(activeValue)
    return candidate !== null && Number.isSafeInteger(candidate) && candidate >= 0 ? candidate : null
  })
  const unknown = sizes.reduce((list, size, index) => (size === null ? [...list, index] : list), [])
  if (flatLength !== null && unknown.length === 1) {
    const knownProduct = sizes.reduce((product, size) => (size === null ? product : product * size), 1)
    if (knownProduct > 0 && flatLength % knownProduct === 0) {
      sizes[unknown[0]] = flatLength / knownProduct
    }
  }
  return sizes
}

function readArrayElement(value, indices, variable, modelVariables, runtimeValues) {
  if (!Array.isArray(indices) || !indices.length) return null
  const array = toArray(value)
  if (!array) return null

  let nested = array
  let nestedSelectionValid = true
  for (let axis = 0; axis < indices.length; axis += 1) {
    const current = toArray(nested)
    if (!current || indices[axis] >= current.length) {
      nestedSelectionValid = false
      break
    }
    nested = current[indices[axis]]
    if (axis < indices.length - 1 && !isArrayLike(nested)) {
      nestedSelectionValid = false
      break
    }
  }
  if (nestedSelectionValid && !isArrayLike(nested)) return nested

  // A nested payload represents explicit dimensions. Do not reinterpret an
  // invalid coordinate as a valid offset into its flattened representation.
  if (array.some((item) => isArrayLike(item))) return null

  if (indices.length === 1) return array[indices[0]] ?? null
  const sizes = getCurrentDimensionSizes(variable, modelVariables, runtimeValues, array.length)
  if (sizes.length !== indices.length || sizes.some((size) => !Number.isSafeInteger(size) || size < 0)) return null
  let linearIndex = 0
  for (let axis = 0; axis < indices.length; axis += 1) {
    if (indices[axis] >= sizes[axis]) return null
    linearIndex = (linearIndex * sizes[axis]) + indices[axis]
  }
  return array[linearIndex] ?? null
}

function flattenArray(value, output = []) {
  const array = toArray(value)
  if (!array) return null
  for (const item of array) {
    if (isArrayLike(item)) {
      if (!flattenArray(item, output)) return null
    } else {
      output.push(item)
    }
  }
  return output
}

function hasFmiArrayShape(variable, expectedLength, modelVariables) {
  const dimensions = getVariableDimensions(variable)
  if (dimensions.length !== 1) return false
  const dimension = dimensions[0]
  if (Number.isInteger(dimension?.start) && dimension.start >= 0) {
    return dimension.start === expectedLength
  }
  if (dimension?.valueReference != null || dimension?.variableName) {
    const limit = getDimensionLimit(dimension, modelVariables)
    return limit === null || limit >= expectedLength
  }
  const startLength = Array.isArray(variable?.start) ? variable.start.length : null
  return startLength === expectedLength
}

export function normalizeFmiSceneBinding(binding, variable, modelVariables = []) {
  if (!binding || typeof binding !== 'object' || Array.isArray(binding)
    || typeof binding.variable !== 'string'
    || binding.variable !== variable?.name
    || !hasValidBindingFields(binding)
    || !FMI_SCENE_BINDING_TARGET_SET.has(binding.target)) return null

  const target = binding.target
  if (Object.prototype.hasOwnProperty.call(binding, 'valueMap')) {
    if (Object.prototype.hasOwnProperty.call(binding, 'visibleWhenGreaterThanOrEqual')) return null
    if (target === 'visible' && Object.values(binding.valueMap).some((value) => typeof value !== 'boolean')) return null
    if (target === 'material.color' && Object.values(binding.valueMap).some((value) => !isBindingColor(value))) return null
    if (!['visible', 'material.color'].includes(target)) return null
  }
  const dimensions = getVariableDimensions(variable)
  if (Object.prototype.hasOwnProperty.call(binding, 'index') && target !== 'weights') return null
  if (VECTOR_TARGET_LENGTHS.has(target)) {
    if (Object.prototype.hasOwnProperty.call(binding, 'indices')
      || !hasFmiArrayShape(variable, VECTOR_TARGET_LENGTHS.get(target), modelVariables)) return null
  } else if (target === 'weights') {
    if (Object.prototype.hasOwnProperty.call(binding, 'indices')) {
      if (!Number.isInteger(binding.index)
        || !isValidArraySelection(binding, variable, modelVariables, false)) return null
    } else if (Number.isInteger(binding.index)) {
      if (dimensions.length) return null
    } else if (!dimensions.length) {
      return null
    } else if (dimensions.length !== 1) {
      return null
    }
  } else if (!isValidArraySelection(binding, variable, modelVariables, false)) {
    return null
  }

  const transformMatch = TRANSFORM_COMPONENT_PATTERN.exec(target)
  const [property] = target.split('.')
  const normalizedProperty = property === 'position' || property === 'translation' ? 'position' : property

  return {
    binding,
    variable,
    target,
    property: normalizedProperty,
    axis: transformMatch?.[1] || null,
    indices: Array.isArray(binding.indices) ? binding.indices : null,
    index: Number.isInteger(binding.index) ? binding.index : null,
    scale: getBindingNumber(binding.scale, 1),
    offset: getBindingNumber(binding.offset, 0),
  }
}

export function getFmiSceneBoundValue(value, normalizedBinding) {
  const numeric = numericValue(value)
  if (numeric === null || !normalizedBinding) return null
  const mapped = numeric * normalizedBinding.scale + normalizedBinding.offset
  return Number.isFinite(mapped) ? mapped : null
}

export function getFmiSceneBindingValue(value, normalizedBinding, modelVariables = [], runtimeValues = {}) {
  if (value === null || value === undefined || !normalizedBinding) return null
  const selected = normalizedBinding.indices
    ? readArrayElement(value, normalizedBinding.indices, normalizedBinding.variable, modelVariables, runtimeValues)
    : value
  if (selected === null || selected === undefined) return null

  const valueMap = normalizedBinding.binding?.valueMap
  if (valueMap && typeof valueMap === 'object' && !Array.isArray(valueMap)) {
    const key = String(selected)
    return Object.prototype.hasOwnProperty.call(valueMap, key) ? valueMap[key] : null
  }

  const vectorLength = VECTOR_TARGET_LENGTHS.get(normalizedBinding.target)
  if (vectorLength || (
    normalizedBinding.target === 'weights'
    && normalizedBinding.index === null
    && normalizedBinding.indices === null
  )) {
    const vector = flattenArray(selected)
    const expectedLength = vectorLength ?? vector?.length
    if (!vector || !expectedLength || vector.length !== expectedLength) return null
    const mapped = vector.map((component) => getFmiSceneBoundValue(component, normalizedBinding))
    if (mapped.some((component) => component === null)) return null
    if (normalizedBinding.target === 'rotation') {
      const length = Math.hypot(...mapped)
      if (!Number.isFinite(length) || length <= 1e-12) return null
      return mapped.map((component) => component / length)
    }
    return mapped
  }
  return selected
}

export function isDraggableFmiSceneBinding(normalizedBinding) {
  const type = String(normalizedBinding?.variable?.type || '').toLowerCase()
  return normalizedBinding?.variable?.causality === 'input'
    && ['position', 'rotation'].includes(normalizedBinding.property)
    && ['x', 'y', 'z'].includes(normalizedBinding.axis)
    && NUMERIC_INPUT_TYPES.has(type)
    && Number.isFinite(normalizedBinding.scale)
    && normalizedBinding.scale !== 0
}

export function getFmiSceneInputValueFromCoordinate(coordinate, normalizedBinding) {
  if (!isDraggableFmiSceneBinding(normalizedBinding)) return null
  const sceneCoordinate = Number(coordinate)
  if (!Number.isFinite(sceneCoordinate) || Math.abs(sceneCoordinate) > MAX_SCENE_COORDINATE) return null

  const variableValue = (sceneCoordinate - normalizedBinding.offset) / normalizedBinding.scale
  if (!Number.isFinite(variableValue)) return null

  const minimum = normalizedBinding.variable.min == null ? null : Number(normalizedBinding.variable.min)
  const maximum = normalizedBinding.variable.max == null ? null : Number(normalizedBinding.variable.max)
  if ((minimum !== null && !Number.isFinite(minimum)) || (maximum !== null && !Number.isFinite(maximum))) return null
  if (minimum !== null && maximum !== null && minimum > maximum) return null

  const type = String(normalizedBinding.variable.type || '').toLowerCase()
  if (!INTEGER_INPUT_TYPES.has(type)) {
    return Math.min(maximum ?? Infinity, Math.max(minimum ?? -Infinity, variableValue))
  }

  const integerMinimum = minimum === null ? null : Math.ceil(minimum)
  const integerMaximum = maximum === null ? null : Math.floor(maximum)
  if (integerMinimum !== null && integerMaximum !== null && integerMinimum > integerMaximum) return null
  return Math.min(integerMaximum ?? Infinity, Math.max(integerMinimum ?? -Infinity, Math.round(variableValue)))
}

export function setFmiSceneArrayElementValue(value, indices, nextValue, variable, modelVariables = [], runtimeValues = {}) {
  const source = flattenArray(value)
  if (!source || !Array.isArray(indices) || indices.length === 0) return null
  const sizes = getCurrentDimensionSizes(variable, modelVariables, runtimeValues, source.length)
  if (sizes.length !== indices.length || sizes.some((size) => !Number.isSafeInteger(size) || size < 0)) return null
  let linearIndex = 0
  for (let axis = 0; axis < indices.length; axis += 1) {
    if (indices[axis] >= sizes[axis]) return null
    linearIndex = (linearIndex * sizes[axis]) + indices[axis]
  }
  if (linearIndex >= source.length) return null
  source[linearIndex] = nextValue
  return source
}
