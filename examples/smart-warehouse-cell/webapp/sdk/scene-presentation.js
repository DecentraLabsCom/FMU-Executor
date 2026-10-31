export const SCENE_PRESENTATION_USER_DATA_KEY = 'decentralabsScenePresentation'
export const SCENE_RENDERING_USER_DATA_KEY = 'decentralabsSceneRendering'
export const SCENE_MATERIAL_RENDERING_USER_DATA_KEY = 'decentralabsMaterialRendering'

const TONE_MAPPING_NAMES = new Set([
  'NoToneMapping',
  'LinearToneMapping',
  'ReinhardToneMapping',
  'CineonToneMapping',
  'ACESFilmicToneMapping',
  'AgXToneMapping',
  'NeutralToneMapping',
])

const COLOR_SPACE_NAMES = new Set([
  'SRGBColorSpace',
  'LinearSRGBColorSpace',
  'DisplayP3ColorSpace',
])

const SHADOW_MAP_NAMES = new Set([
  'BasicShadowMap',
  'PCFShadowMap',
  'PCFSoftShadowMap',
  'VSMShadowMap',
])

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isFiniteVector(value) {
  return Array.isArray(value) && value.length === 3 && value.every(Number.isFinite)
}

function isColor(value) {
  return value === null || (typeof value === 'string' && value.trim().length > 0)
}

function isColorString(value) {
  return typeof value === 'string' && value.trim().length > 0
}

function normalizeCamera(camera) {
  if (camera === undefined) return undefined
  if (!isRecord(camera) || !isFiniteVector(camera.position) || !isFiniteVector(camera.target)) return null
  const projection = camera.projection ?? 'perspective'
  if (projection !== 'perspective') return null
  if (!Number.isFinite(camera.fov) || camera.fov <= 0 || camera.fov >= 180) return null
  if (camera.up !== undefined && !isFiniteVector(camera.up)) return null
  if (camera.zoom !== undefined && (!Number.isFinite(camera.zoom) || camera.zoom <= 0)) return null
  if (camera.filmGauge !== undefined && (!Number.isFinite(camera.filmGauge) || camera.filmGauge <= 0)) return null
  if (camera.filmOffset !== undefined && !Number.isFinite(camera.filmOffset)) return null
  if (camera.aspectRatio !== undefined && (!Number.isFinite(camera.aspectRatio) || camera.aspectRatio <= 0)) return null
  if (!Number.isFinite(camera.near) || camera.near <= 0 || !Number.isFinite(camera.far) || camera.far <= camera.near) return null
  for (const key of ['minDistance', 'maxDistance', 'minPolarAngle', 'maxPolarAngle', 'dampingFactor', 'rotateSpeed', 'zoomSpeed', 'panSpeed']) {
    if (camera[key] !== undefined && !Number.isFinite(camera[key])) return null
  }
  for (const key of ['enableDamping', 'enablePan', 'enableRotate', 'enableZoom', 'screenSpacePanning']) {
    if (camera[key] !== undefined && typeof camera[key] !== 'boolean') return null
  }
  if (camera.minDistance !== undefined && camera.minDistance < 0) return null
  if (camera.maxDistance !== undefined && camera.maxDistance <= 0) return null
  if (camera.minDistance !== undefined && camera.maxDistance !== undefined && camera.maxDistance < camera.minDistance) return null
  if (camera.minPolarAngle !== undefined && (camera.minPolarAngle < 0 || camera.minPolarAngle > Math.PI)) return null
  if (camera.maxPolarAngle !== undefined && (camera.maxPolarAngle < 0 || camera.maxPolarAngle > Math.PI)) return null
  if (camera.dampingFactor !== undefined && (camera.dampingFactor < 0 || camera.dampingFactor > 1)) return null
  let follow
  if (camera.follow !== undefined) {
    if (!isRecord(camera.follow) || typeof camera.follow.node !== 'string' || !camera.follow.node.trim()) return null
    if (camera.follow.positionOffset !== undefined && !isFiniteVector(camera.follow.positionOffset)) return null
    if (camera.follow.targetOffset !== undefined && !isFiniteVector(camera.follow.targetOffset)) return null
    follow = {
      node: camera.follow.node,
      ...(camera.follow.positionOffset && { positionOffset: [...camera.follow.positionOffset] }),
      ...(camera.follow.targetOffset && { targetOffset: [...camera.follow.targetOffset] }),
    }
  }
  return {
    ...(camera.projection !== undefined && { projection }),
    position: [...camera.position],
    target: [...camera.target],
    fov: camera.fov,
    ...(camera.up && { up: [...camera.up] }),
    ...(camera.zoom !== undefined && { zoom: camera.zoom }),
    ...(camera.filmGauge !== undefined && { filmGauge: camera.filmGauge }),
    ...(camera.filmOffset !== undefined && { filmOffset: camera.filmOffset }),
    near: camera.near,
    far: camera.far,
    ...(camera.aspectRatio !== undefined && { aspectRatio: camera.aspectRatio }),
    ...(follow && { follow }),
    ...Object.fromEntries(['minDistance', 'maxDistance', 'minPolarAngle', 'maxPolarAngle', 'dampingFactor', 'enableDamping', 'enablePan', 'enableRotate', 'enableZoom', 'screenSpacePanning', 'rotateSpeed', 'zoomSpeed', 'panSpeed']
      .filter((key) => camera[key] !== undefined)
      .map((key) => [key, camera[key]])),
  }
}

function normalizeFog(fog) {
  if (fog === undefined || fog === null) return fog
  if (!isRecord(fog) || !isColorString(fog.color)) return null
  if (fog.type === 'exp2') {
    if (!Number.isFinite(fog.density) || fog.density < 0) return null
    return { type: 'exp2', color: fog.color, density: fog.density }
  }
  if (fog.type !== undefined && fog.type !== 'linear') return null
  if (!Number.isFinite(fog.near) || !Number.isFinite(fog.far) || fog.near < 0 || fog.far <= fog.near) return null
  return { color: fog.color, near: fog.near, far: fog.far }
}

function normalizeLighting(lighting) {
  if (lighting === undefined) return undefined
  if (!isRecord(lighting) || !['authored', 'studio'].includes(lighting.mode)) return null
  const environment = lighting.environment ?? (lighting.mode === 'studio' ? 'room' : 'none')
  if (!['none', 'room'].includes(environment)) return null
  const ambient = lighting.ambient ?? []
  if (!Array.isArray(ambient)) return null
  const normalizedAmbient = []
  for (const light of ambient) {
    if (!isRecord(light) || !['hemisphere', 'ambient'].includes(light.type) || !Number.isFinite(light.intensity) || light.intensity < 0) return null
    if (light.type === 'hemisphere') {
      if (!isColorString(light.skyColor) || !isColorString(light.groundColor)) return null
      if (light.position !== undefined && !isFiniteVector(light.position)) return null
      normalizedAmbient.push({ type: light.type, skyColor: light.skyColor, groundColor: light.groundColor, intensity: light.intensity, ...(light.position && { position: [...light.position] }) })
    } else {
      if (!isColorString(light.color)) return null
      normalizedAmbient.push({ type: light.type, color: light.color, intensity: light.intensity })
    }
  }
  if (lighting.environmentIntensity !== undefined && (!Number.isFinite(lighting.environmentIntensity) || lighting.environmentIntensity < 0)) return null
  return {
    mode: lighting.mode,
    environment,
    ...(lighting.environmentIntensity !== undefined && { environmentIntensity: lighting.environmentIntensity }),
    ambient: normalizedAmbient,
  }
}

export function normalizeScenePresentation(presentation) {
  if (!isRecord(presentation) || presentation.version !== 1) return null

  const camera = normalizeCamera(presentation.camera)
  if (camera === null) return null

  const scene = presentation.scene
  if (scene !== undefined && (
    !isRecord(scene)
    || !isColor(scene.background)
    || (scene.backgroundImage !== undefined && (typeof scene.backgroundImage !== 'string' || !scene.backgroundImage.startsWith('data:image/')))
    || (scene.backgroundMapping !== undefined && !['UVMapping', 'EquirectangularReflectionMapping'].includes(scene.backgroundMapping))
    || (scene.backgroundColorSpace !== undefined && !COLOR_SPACE_NAMES.has(scene.backgroundColorSpace))
  )) return null
  const fog = normalizeFog(scene?.fog)
  if (fog === null) return null

  const renderer = presentation.renderer
  if (renderer !== undefined) {
    if (!isRecord(renderer)) return null
    if (renderer.toneMapping !== undefined && !TONE_MAPPING_NAMES.has(renderer.toneMapping)) return null
    if (renderer.outputColorSpace !== undefined && !COLOR_SPACE_NAMES.has(renderer.outputColorSpace)) return null
    if (renderer.toneMappingExposure !== undefined && !Number.isFinite(renderer.toneMappingExposure)) return null
    if (renderer.pixelRatio !== undefined && (!Number.isFinite(renderer.pixelRatio) || renderer.pixelRatio <= 0)) return null
    if (renderer.pixelRatioLimit !== undefined && (!Number.isFinite(renderer.pixelRatioLimit) || renderer.pixelRatioLimit <= 0)) return null
    if (renderer.clearColor !== undefined && !isColor(renderer.clearColor)) return null
    if (renderer.clearAlpha !== undefined && (!Number.isFinite(renderer.clearAlpha) || renderer.clearAlpha < 0 || renderer.clearAlpha > 1)) return null
    for (const key of ['autoClear', 'autoClearColor', 'autoClearDepth', 'autoClearStencil', 'sortObjects']) {
      if (renderer[key] !== undefined && typeof renderer[key] !== 'boolean') return null
    }
    if (renderer.shadowMap !== undefined) {
      if (!isRecord(renderer.shadowMap) || typeof renderer.shadowMap.enabled !== 'boolean') return null
      if (renderer.shadowMap.type !== undefined && !SHADOW_MAP_NAMES.has(renderer.shadowMap.type)) return null
    }
  }

  const lighting = normalizeLighting(presentation.lighting)
  if (lighting === null) return null

  return {
    version: 1,
    ...(camera && { camera }),
    ...(scene && { scene: {
      background: scene.background,
      ...(scene.backgroundImage && { backgroundImage: scene.backgroundImage }),
      ...(scene.backgroundMapping && { backgroundMapping: scene.backgroundMapping }),
      ...(scene.backgroundColorSpace && { backgroundColorSpace: scene.backgroundColorSpace }),
      ...(fog !== undefined && { fog }),
    } }),
    ...(renderer && { renderer: {
      ...(renderer.toneMapping !== undefined && { toneMapping: renderer.toneMapping }),
      ...(renderer.outputColorSpace !== undefined && { outputColorSpace: renderer.outputColorSpace }),
      ...(renderer.toneMappingExposure !== undefined && { toneMappingExposure: renderer.toneMappingExposure }),
      ...(renderer.pixelRatio !== undefined && { pixelRatio: renderer.pixelRatio }),
      ...(renderer.pixelRatioLimit !== undefined && { pixelRatioLimit: renderer.pixelRatioLimit }),
      ...(renderer.clearColor !== undefined && { clearColor: renderer.clearColor }),
      ...(renderer.clearAlpha !== undefined && { clearAlpha: renderer.clearAlpha }),
      ...Object.fromEntries(['autoClear', 'autoClearColor', 'autoClearDepth', 'autoClearStencil', 'sortObjects']
        .filter((key) => renderer[key] !== undefined)
        .map((key) => [key, renderer[key]])),
      ...(renderer.shadowMap !== undefined && { shadowMap: { ...renderer.shadowMap } }),
    } }),
    ...(lighting && { lighting }),
  }
}

export function readScenePresentation(sceneOrUserData) {
  const userData = sceneOrUserData?.userData || sceneOrUserData
  return normalizeScenePresentation(userData?.[SCENE_PRESENTATION_USER_DATA_KEY])
}

function getObjectRenderingState(node) {
  const state = {}
  if (node.isMesh) {
    state.castShadow = node.castShadow === true
    state.receiveShadow = node.receiveShadow === true
  } else if (node.isLight) {
    state.castShadow = node.castShadow === true
    const shadow = node.shadow
    if (shadow) {
      state.shadow = {
        mapSize: [shadow.mapSize?.x, shadow.mapSize?.y],
        bias: shadow.bias,
        normalBias: shadow.normalBias,
        radius: shadow.radius,
        camera: shadow.camera && {
          left: shadow.camera.left,
          right: shadow.camera.right,
          top: shadow.camera.top,
          bottom: shadow.camera.bottom,
          near: shadow.camera.near,
          far: shadow.camera.far,
        },
      }
    }
  }
  if (node.visible === false) state.visible = false
  if (Number.isFinite(node.renderOrder) && node.renderOrder !== 0) state.renderOrder = node.renderOrder
  return Object.keys(state).length > 0 ? state : null
}

function serializeUserDataForGltf(scene) {
  const originals = []
  scene.traverse?.((node) => {
    const materials = Array.isArray(node.material) ? node.material : [node.material]
    for (const target of [node, ...materials.filter(Boolean)]) {
      if (!isRecord(target.userData)) continue
      const serializable = {}
      for (const [key, value] of Object.entries(target.userData)) {
        try {
          if (JSON.stringify(value) !== undefined) serializable[key] = value
        } catch {
          // Keep other extras when one runtime-only value is cyclic.
        }
      }
      if (Object.keys(serializable).length === Object.keys(target.userData).length) continue
      originals.push({ target, userData: target.userData })
      target.userData = serializable
    }
  })
  return () => {
    for (const { target, userData } of originals) target.userData = userData
  }
}

function orientDirectionalLightsForGltf(scene, THREE) {
  if (!THREE?.Vector3 || !THREE?.Quaternion) return () => {}
  scene.updateMatrixWorld?.(true)
  const lights = []
  scene.traverse?.((light) => {
    if ((!light.isDirectionalLight && !light.isSpotLight) || !light.target || light.target.parent === light) return
    light.updateWorldMatrix?.(true, false)
    light.target.updateWorldMatrix?.(true, false)
    lights.push({
      light,
      lightQuaternion: light.quaternion.clone(),
      lightPosition: light.getWorldPosition(new THREE.Vector3()),
      targetPosition: light.target.getWorldPosition(new THREE.Vector3()),
      targetParent: light.target.parent,
      targetIndex: light.target.parent?.children.indexOf(light.target) ?? -1,
      targetPositionLocal: light.target.position.clone(),
      targetQuaternion: light.target.quaternion.clone(),
      targetScale: light.target.scale.clone(),
    })
  })
  for (const { light, lightPosition, targetPosition } of lights) {
    const direction = targetPosition.sub(lightPosition).normalize()
    if (direction.lengthSq() === 0) continue

    const parentRotation = new THREE.Quaternion()
    light.parent?.getWorldQuaternion?.(parentRotation)
    direction.applyQuaternion(parentRotation.invert()).normalize()
    light.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, -1), direction)

    const target = light.target
    target.parent?.remove(target)
    light.add(target)
    target.position.set(0, 0, -1)
    target.rotation.set(0, 0, 0)
    target.scale.set(1, 1, 1)
  }
  scene.updateMatrixWorld?.(true)
  return () => {
    for (const { light, lightQuaternion, targetParent, targetIndex, targetPositionLocal, targetQuaternion, targetScale } of lights) {
      const target = light.target
      target.parent?.remove(target)
      if (targetParent) {
        targetParent.add(target)
        if (targetIndex >= 0) {
          const currentIndex = targetParent.children.indexOf(target)
          targetParent.children.splice(currentIndex, 1)
          targetParent.children.splice(Math.min(targetIndex, targetParent.children.length), 0, target)
        }
      }
      target.position.copy(targetPositionLocal)
      target.quaternion.copy(targetQuaternion)
      target.scale.copy(targetScale)
      light.quaternion.copy(lightQuaternion)
    }
    scene.updateMatrixWorld?.(true)
  }
}

export function attachScenePresentation(scene, presentation, THREE) {
  const normalized = normalizeScenePresentation(presentation)
  if (!normalized) throw new TypeError('A valid version 1 scene presentation is required')
  if (!scene || typeof scene !== 'object') throw new TypeError('A Three.js scene is required')
  scene.userData ||= {}
  scene.userData[SCENE_PRESENTATION_USER_DATA_KEY] = normalized
  scene.traverse?.((node) => {
    const renderingState = getObjectRenderingState(node)
    if (!renderingState) return
    node.userData ||= {}
    node.userData[SCENE_RENDERING_USER_DATA_KEY] = renderingState
  })
  return normalized
}

function colorHex(color) {
  if (!color || typeof color.getHexString !== 'function') return null
  return `#${color.getHexString()}`.toLowerCase()
}

function ambientDescriptorFor(light, THREE) {
  if (light.isHemisphereLight) {
    light.updateWorldMatrix?.(true, false)
    const position = THREE?.Vector3 && light.getWorldPosition?.(new THREE.Vector3())
    return {
      type: 'hemisphere',
      skyColor: colorHex(light.color),
      groundColor: colorHex(light.groundColor),
      intensity: light.intensity,
      ...(position && { position: position.toArray() }),
    }
  }
  if (light.isAmbientLight) {
    return { type: 'ambient', color: colorHex(light.color), intensity: light.intensity }
  }
  return null
}

function vectorArray(vector) {
  if (Array.isArray(vector)) return vector.length === 3 && vector.every(Number.isFinite) ? [...vector] : null
  if (typeof vector?.toArray === 'function') {
    const values = vector.toArray()
    return values.length === 3 && values.every(Number.isFinite) ? values : null
  }
  if ([vector?.x, vector?.y, vector?.z].every(Number.isFinite)) return [vector.x, vector.y, vector.z]
  return null
}

function canvasDataUrl(image) {
  if (typeof image?.toDataURL === 'function') return image.toDataURL('image/png')
  if (typeof document === 'undefined' || !image) return null
  const width = image.naturalWidth || image.videoWidth || image.width
  const height = image.naturalHeight || image.videoHeight || image.height
  if (!width || !height) return null
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  canvas.getContext('2d').drawImage(image, 0, 0, width, height)
  return canvas.toDataURL('image/png')
}

function assertNoSceneOverlays(surfaceElement) {
  if (!surfaceElement) return []
  if (typeof surfaceElement.querySelectorAll !== 'function') return
  const marked = [
    ...(surfaceElement.matches?.('[data-decentralabs-scene-overlay]') ? [surfaceElement] : []),
    ...surfaceElement.querySelectorAll('[data-decentralabs-scene-overlay]'),
  ]
  if (marked.length > 0) throw new TypeError('DOM effects over the Three.js canvas are outside GLB scene presentation; remove them before export')
}

function cameraTarget(camera, controls, explicitTarget, existingCamera) {
  const controlsTarget = vectorArray(controls?.target)
  const requestedTarget = vectorArray(explicitTarget)
  const profileTarget = vectorArray(existingCamera?.target)
  const target = controlsTarget || requestedTarget || profileTarget
  if (!target) throw new TypeError('Scene presentation capture requires OrbitControls.target or an explicit camera target')
  return target
}

export function captureScenePresentation(THREE, {
  scene,
  camera,
  renderer,
  controls,
  target,
  surfaceElement,
  lighting,
} = {}) {
  if (!scene || !camera || !renderer) throw new TypeError('Scene presentation capture requires a scene, active camera, and renderer')
  if (!camera.isPerspectiveCamera) throw new TypeError('Scene presentation capture currently supports PerspectiveCamera only')
  if (camera.parent && camera.parent !== scene) throw new TypeError('The shared GLB viewer cannot reproduce a camera nested under another transform')
  if (camera.view?.enabled) throw new TypeError('PerspectiveCamera view offsets are not represented in the scene profile')
  if (camera.layers?.mask !== undefined && camera.layers.mask !== 1) throw new TypeError('Camera layer masks are not represented in glTF')
  assertNoSceneOverlays(surfaceElement)
  const existing = readScenePresentation(scene)
  const worldPosition = camera.getWorldPosition?.(new THREE.Vector3()) || camera.position
  const capturedTarget = cameraTarget(camera, controls, target, existing?.camera)
  const cameraState = {
    projection: 'perspective',
    position: vectorArray(worldPosition),
    target: capturedTarget,
    fov: camera.fov,
    up: vectorArray(camera.up),
    zoom: camera.zoom,
    filmGauge: camera.filmGauge,
    filmOffset: camera.filmOffset,
    near: camera.near,
    far: camera.far,
    aspectRatio: camera.aspect,
    ...(controls && Object.fromEntries(['minDistance', 'maxDistance', 'minPolarAngle', 'maxPolarAngle', 'dampingFactor', 'enableDamping', 'enablePan', 'enableRotate', 'enableZoom', 'screenSpacePanning', 'rotateSpeed', 'zoomSpeed', 'panSpeed']
      .filter((key) => controls[key] !== undefined)
      .map((key) => [key, controls[key]]))),
  }
  const sceneState = { background: null }
  if (scene.background?.isColor) sceneState.background = colorHex(scene.background)
  else if (scene.background?.isTexture) {
    if (![THREE.UVMapping, THREE.EquirectangularReflectionMapping].includes(scene.background.mapping)) {
      throw new TypeError('The scene background uses a mapping that the GLB viewer cannot reproduce')
    }
    const image = canvasDataUrl(scene.background.image || scene.background.source?.data)
    if (!image) throw new TypeError('The scene background texture cannot be serialized as a portable image')
    sceneState.backgroundImage = image
    sceneState.backgroundMapping = scene.background.mapping === THREE.EquirectangularReflectionMapping
      ? 'EquirectangularReflectionMapping'
      : 'UVMapping'
    for (const name of COLOR_SPACE_NAMES) {
      if (THREE[name] === scene.background.colorSpace) sceneState.backgroundColorSpace = name
    }
  } else if (scene.background !== null && scene.background !== undefined) {
    throw new TypeError('The active scene background is not a portable color or image texture')
  }
  if (scene.fog?.isFogExp2) sceneState.fog = { type: 'exp2', color: colorHex(scene.fog.color), density: scene.fog.density }
  else if (scene.fog) sceneState.fog = { color: colorHex(scene.fog.color), near: scene.fog.near, far: scene.fog.far }

  const clearColor = renderer.getClearColor?.(new THREE.Color())
  const rendererState = {
    outputColorSpace: Object.entries(THREE).find(([name, value]) => name.endsWith('ColorSpace') && value === renderer.outputColorSpace)?.[0],
    toneMapping: Object.entries(THREE).find(([name, value]) => name.endsWith('ToneMapping') && value === renderer.toneMapping)?.[0],
    toneMappingExposure: renderer.toneMappingExposure,
    pixelRatio: renderer.getPixelRatio?.(),
    ...(clearColor && { clearColor: colorHex(clearColor) }),
    ...(renderer.getClearAlpha && { clearAlpha: renderer.getClearAlpha() }),
    ...(renderer.shadowMap && {
      shadowMap: {
        enabled: renderer.shadowMap.enabled === true,
        ...(Object.entries(THREE).find(([name, value]) => name.endsWith('ShadowMap') && value === renderer.shadowMap.type)?.[0]
          ? { type: Object.entries(THREE).find(([name, value]) => name.endsWith('ShadowMap') && value === renderer.shadowMap.type)[0] }
          : {}),
      },
    }),
    ...Object.fromEntries(['autoClear', 'autoClearColor', 'autoClearDepth', 'autoClearStencil', 'sortObjects']
      .filter((key) => typeof renderer[key] === 'boolean')
      .map((key) => [key, renderer[key]])),
  }
  for (const key of Object.keys(rendererState)) if (rendererState[key] === undefined || rendererState[key] === null) delete rendererState[key]
  const contextAttributes = renderer.getContext?.()?.getContextAttributes?.()
  if (contextAttributes && (contextAttributes.antialias !== true || contextAttributes.alpha !== false)) {
    throw new TypeError('The shared GLB viewer requires antialiasing enabled and an opaque WebGL canvas')
  }
  if (renderer.localClippingEnabled) throw new TypeError('Renderer local clipping is not represented in glTF')

  const ambient = []
  scene.traverse?.((node) => {
    const descriptor = ambientDescriptorFor(node, THREE)
    if (descriptor) ambient.push(descriptor)
  })
  const priorLighting = existing?.lighting
  const requestedLighting = lighting || {}
  if (scene.environment && (requestedLighting.environment ?? priorLighting?.environment) !== 'room') {
    throw new TypeError('Custom Three.js environment textures are not portable; use the shared room environment or export a supported image environment')
  }
  const lightingState = {
    mode: requestedLighting.mode || priorLighting?.mode || 'authored',
    environment: requestedLighting.environment || priorLighting?.environment || (requestedLighting.mode === 'studio' ? 'room' : 'none'),
    ...(requestedLighting.environmentIntensity !== undefined
      ? { environmentIntensity: requestedLighting.environmentIntensity }
      : priorLighting?.environmentIntensity !== undefined
        ? { environmentIntensity: priorLighting.environmentIntensity }
        : {}),
    ambient,
  }
  const captured = normalizeScenePresentation({
    version: 1,
    camera: cameraState,
    scene: sceneState,
    renderer: rendererState,
    lighting: lightingState,
  })
  if (!captured) throw new TypeError('The active Three.js presentation contains unsupported or invalid state')
  return captured
}

function sameAmbientLight(left, right) {
  if (!left || !right || left.type !== right.type || left.intensity !== right.intensity) return false
  return left.type === 'hemisphere'
    ? left.skyColor?.toLowerCase() === right.skyColor?.toLowerCase()
      && left.groundColor?.toLowerCase() === right.groundColor?.toLowerCase()
      && (!left.position || !right.position || left.position.every((value, index) => value === right.position[index]))
    : left.color?.toLowerCase() === right.color?.toLowerCase()
}

function sideName(THREE, side) {
  return ['FrontSide', 'BackSide', 'DoubleSide'].find((name) => THREE?.[name] === side)
}

function getMaterialRenderingState(material, THREE) {
  return {
    visible: material.visible,
    depthTest: material.depthTest,
    depthWrite: material.depthWrite,
    colorWrite: material.colorWrite,
    toneMapped: material.toneMapped,
    wireframe: material.wireframe === true,
    flatShading: material.flatShading === true,
    alphaToCoverage: material.alphaToCoverage === true,
    premultipliedAlpha: material.premultipliedAlpha === true,
    envMapIntensity: material.envMapIntensity,
    ...(sideName(THREE, material.side) && { side: sideName(THREE, material.side) }),
    polygonOffset: material.polygonOffset === true,
    polygonOffsetFactor: material.polygonOffsetFactor,
    polygonOffsetUnits: material.polygonOffsetUnits,
  }
}

function captureMaterialMetadata(scene, THREE) {
  const originals = []
  const restore = () => {
    for (const { material, userData, previous } of originals) {
      if (userData) {
        if (previous === undefined) delete userData[SCENE_MATERIAL_RENDERING_USER_DATA_KEY]
        else userData[SCENE_MATERIAL_RENDERING_USER_DATA_KEY] = previous
        material.userData = userData
      } else {
        material.userData = undefined
      }
    }
  }
  try {
    scene.traverse?.((node) => {
      const materials = Array.isArray(node.material) ? node.material : [node.material]
      for (const material of materials) {
        if (!material) continue
        if (material.isShaderMaterial || material.isRawShaderMaterial) {
          throw new TypeError('ShaderMaterial cannot be represented faithfully in glTF')
        }
        if (THREE?.Material && material.onBeforeCompile !== THREE.Material.prototype.onBeforeCompile) {
          throw new TypeError('Custom onBeforeCompile materials cannot be represented faithfully in glTF')
        }
        if (material.blending !== undefined && THREE?.NormalBlending !== undefined && material.blending !== THREE.NormalBlending) {
          throw new TypeError('Non-normal material blending cannot be represented faithfully in glTF')
        }
        if (material.clippingPlanes?.length) throw new TypeError('Custom material clipping planes are not represented in glTF')
        if (THREE?.LessEqualDepth !== undefined && material.depthFunc !== THREE.LessEqualDepth) {
          throw new TypeError('Custom material depth functions are not represented in glTF')
        }
        if (material.stencilWrite) throw new TypeError('Material stencil operations are not represented in glTF')
        const previous = material.userData?.[SCENE_MATERIAL_RENDERING_USER_DATA_KEY]
        originals.push({ material, userData: material.userData, previous })
        material.userData ||= {}
        material.userData[SCENE_MATERIAL_RENDERING_USER_DATA_KEY] = getMaterialRenderingState(material, THREE)
      }
    })
    return restore
  } catch (error) {
    restore()
    throw error
  }
}

function createGlbLineMaterial(THREE, material) {
  return new THREE.MeshBasicMaterial({
    color: material.color,
    vertexColors: material.vertexColors,
    opacity: material.opacity,
    transparent: material.transparent,
    alphaTest: material.alphaTest,
    side: material.side,
    depthTest: material.depthTest,
    depthWrite: material.depthWrite,
    colorWrite: material.colorWrite,
    toneMapped: material.toneMapped,
  })
}

export async function exportSceneWithPresentation({
  THREE,
  scene,
  camera,
  renderer,
  controls,
  target,
  surfaceElement,
  lighting,
  exporter,
  options = {},
} = {}) {
  if (typeof exporter?.parseAsync !== 'function') throw new TypeError('A Three.js GLTFExporter instance is required')
  if (options.onlyVisible === true) throw new TypeError('Presentation exports must include hidden scene geometry')
  if (!THREE || !scene || !camera || !renderer) throw new TypeError('Export requires THREE, scene, active camera, and renderer')

  const presentation = captureScenePresentation(THREE, { scene, camera, renderer, controls, target, surfaceElement, lighting })
  const restoreMaterialMetadata = captureMaterialMetadata(scene, THREE)
  try {
    attachScenePresentation(scene, presentation, THREE)
  } catch (error) {
    restoreMaterialMetadata()
    throw error
  }
  const ambientNodes = []
  scene.traverse?.((node) => {
    if (node.isHemisphereLight || node.isAmbientLight) ambientNodes.push(node)
  })
  const detachedAmbientLights = ambientNodes
    .filter((node) => node.parent)
    .map((node) => ({ node, parent: node.parent, index: node.parent.children.indexOf(node) }))
  const lineMaterials = []
  let restoreUserData = () => {}
  if (THREE?.MeshBasicMaterial) {
    scene.traverse?.((node) => {
      const originalMaterial = node.material
      const sourceMaterials = Array.isArray(node.material) ? node.material : [node.material]
      if (!sourceMaterials.some((material) => material?.isLineBasicMaterial)) return
      const temporaryMaterials = []
      const convert = (material) => material?.isLineBasicMaterial
        ? (() => {
          const converted = createGlbLineMaterial(THREE, material)
          converted.userData = { ...material.userData }
          temporaryMaterials.push(converted)
          return converted
        })()
        : material
      node.material = Array.isArray(node.material) ? node.material.map(convert) : convert(node.material)
      lineMaterials.push({ node, originalMaterial, temporaryMaterials })
    })
  }
  for (const { node, parent } of detachedAmbientLights) parent.remove(node)
  let restoreLights = () => {}

  try {
    restoreUserData = serializeUserDataForGltf(scene)
    restoreLights = orientDirectionalLightsForGltf(scene, THREE)
    const result = await exporter.parseAsync(scene, { ...options, onlyVisible: false })
    // GLTFExporter keeps matrix.elements by reference in non-binary JSON output.
    // Snapshot those arrays before restoring temporary light aim, without changing
    // the caller's choice between glTF matrix and TRS transforms.
    if (options.binary !== true && Array.isArray(result?.nodes)) {
      for (const node of result.nodes) {
        if (Array.isArray(node.matrix)) node.matrix = [...node.matrix]
      }
    }
    return result
  } finally {
    restoreLights()
    restoreUserData()
    for (const { node, originalMaterial, temporaryMaterials } of lineMaterials) {
      for (const material of temporaryMaterials) material.dispose()
      node.material = originalMaterial
    }
    for (const { node, parent, index } of detachedAmbientLights) {
      parent.add(node)
      const currentIndex = parent.children.indexOf(node)
      parent.children.splice(currentIndex, 1)
      parent.children.splice(Math.min(index, parent.children.length), 0, node)
    }
    restoreMaterialMetadata()
  }
}

export function applyScenePresentation(THREE, presentation, { scene, camera, renderer, controls } = {}) {
  const profile = normalizeScenePresentation(presentation)
  if (!profile) {
    return {
      valid: false,
      lightingMode: 'studio',
      environment: 'room',
      useDefaultLighting: true,
      useAutomaticCameraFraming: true,
    }
  }

  if (profile.scene && scene) {
    if (profile.scene.backgroundImage) {
      if (!scene.background?.isTexture) {
        const texture = new THREE.TextureLoader().load(profile.scene.backgroundImage)
        if (profile.scene.backgroundMapping) texture.mapping = THREE[profile.scene.backgroundMapping]
        if (profile.scene.backgroundColorSpace) texture.colorSpace = THREE[profile.scene.backgroundColorSpace]
        scene.background = texture
      }
    } else {
      scene.background = profile.scene.background === null ? null : new THREE.Color(profile.scene.background)
    }
    scene.fog = profile.scene.fog
      ? profile.scene.fog.type === 'exp2'
        ? new THREE.FogExp2(profile.scene.fog.color, profile.scene.fog.density)
        : new THREE.Fog(profile.scene.fog.color, profile.scene.fog.near, profile.scene.fog.far)
      : null
  }
  if (profile.camera && camera) {
    camera.position.set(...profile.camera.position)
    camera.fov = profile.camera.fov
    if (profile.camera.up) camera.up.set(...profile.camera.up)
    if (profile.camera.zoom !== undefined) camera.zoom = profile.camera.zoom
    if (profile.camera.filmGauge !== undefined) camera.filmGauge = profile.camera.filmGauge
    if (profile.camera.filmOffset !== undefined) camera.filmOffset = profile.camera.filmOffset
    camera.near = profile.camera.near
    camera.far = profile.camera.far
    camera.lookAt(...profile.camera.target)
    camera.updateProjectionMatrix()
    if (controls) {
      controls.target.set(...profile.camera.target)
      for (const key of ['minDistance', 'maxDistance', 'minPolarAngle', 'maxPolarAngle', 'dampingFactor', 'enableDamping', 'enablePan', 'enableRotate', 'enableZoom', 'screenSpacePanning', 'rotateSpeed', 'zoomSpeed', 'panSpeed']) {
        if (profile.camera[key] !== undefined) controls[key] = profile.camera[key]
      }
    }
  }
  if (profile.renderer && renderer) {
    if (profile.renderer.toneMapping !== undefined) renderer.toneMapping = THREE[profile.renderer.toneMapping]
    if (profile.renderer.outputColorSpace !== undefined) renderer.outputColorSpace = THREE[profile.renderer.outputColorSpace]
    if (profile.renderer.toneMappingExposure !== undefined) renderer.toneMappingExposure = profile.renderer.toneMappingExposure
    if (profile.renderer.pixelRatio !== undefined && typeof renderer.setPixelRatio === 'function') {
      renderer.setPixelRatio(profile.renderer.pixelRatio)
    } else if (profile.renderer.pixelRatioLimit !== undefined && typeof renderer.getPixelRatio === 'function' && typeof renderer.setPixelRatio === 'function') {
      renderer.setPixelRatio(Math.min(renderer.getPixelRatio(), profile.renderer.pixelRatioLimit))
    }
    if (profile.renderer.clearColor !== undefined && typeof renderer.setClearColor === 'function') {
      renderer.setClearColor(profile.renderer.clearColor === null ? 0x000000 : new THREE.Color(profile.renderer.clearColor), profile.renderer.clearAlpha ?? 1)
    }
    for (const key of ['autoClear', 'autoClearColor', 'autoClearDepth', 'autoClearStencil', 'sortObjects']) {
      if (profile.renderer[key] !== undefined) renderer[key] = profile.renderer[key]
    }
    if (profile.renderer.shadowMap) {
      renderer.shadowMap.enabled = profile.renderer.shadowMap.enabled
      if (profile.renderer.shadowMap.type !== undefined) renderer.shadowMap.type = THREE[profile.renderer.shadowMap.type]
    }
  }
  if (profile.lighting?.environmentIntensity !== undefined && scene) {
    scene.environmentIntensity = profile.lighting.environmentIntensity
  }
  for (const light of profile.lighting?.ambient || []) {
    const alreadyInScene = [...(scene?.children || [])].some((node) => {
      const existing = ambientDescriptorFor(node, THREE)
      return sameAmbientLight(existing, light)
    })
    if (alreadyInScene) continue
    if (light.type === 'hemisphere') {
      const hemisphere = new THREE.HemisphereLight(light.skyColor, light.groundColor, light.intensity)
      if (light.position) hemisphere.position.set(...light.position)
      scene?.add(hemisphere)
    } else if (light.type === 'ambient') {
      scene?.add(new THREE.AmbientLight(light.color, light.intensity))
    }
  }

  const lightingMode = profile.lighting?.mode || 'studio'
  const environment = profile.lighting?.environment || (lightingMode === 'studio' ? 'room' : 'none')
  return {
    valid: true,
    lightingMode,
    environment,
    useDefaultLighting: lightingMode !== 'authored',
    useAutomaticCameraFraming: !profile.camera,
  }
}

export function applySceneObjectRendering(root) {
  let applied = 0
  root?.traverse?.((node) => {
    const state = node.userData?.[SCENE_RENDERING_USER_DATA_KEY]
    if (!isRecord(state)) return
    let objectApplied = false
    if (typeof state.visible === 'boolean') {
      node.visible = state.visible
      objectApplied = true
    }
    if (Number.isFinite(state.renderOrder)) {
      node.renderOrder = state.renderOrder
      objectApplied = true
    }
    if (node.isMesh) {
      if (typeof state.castShadow === 'boolean') node.castShadow = state.castShadow
      if (typeof state.receiveShadow === 'boolean') node.receiveShadow = state.receiveShadow
      objectApplied = true
    } else if (node.isLight) {
      if (typeof state.castShadow === 'boolean') node.castShadow = state.castShadow
      if (isRecord(state.shadow) && node.shadow) {
        const { mapSize, bias, normalBias, radius, camera } = state.shadow
        if (Array.isArray(mapSize) && mapSize.length === 2 && mapSize.every(Number.isFinite)) node.shadow.mapSize.set(...mapSize)
        if (Number.isFinite(bias)) node.shadow.bias = bias
        if (Number.isFinite(normalBias)) node.shadow.normalBias = normalBias
        if (Number.isFinite(radius)) node.shadow.radius = radius
        if (isRecord(camera)) {
          for (const key of ['left', 'right', 'top', 'bottom', 'near', 'far']) {
            if (Number.isFinite(camera[key])) node.shadow.camera[key] = camera[key]
          }
          node.shadow.camera.updateProjectionMatrix?.()
        }
      }
      objectApplied = true
    }
    if (objectApplied) applied += 1
  })
  return applied
}

export function applySceneMaterialRendering(root, THREE) {
  let applied = 0
  root?.traverse?.((node) => {
    const materials = Array.isArray(node.material) ? node.material : [node.material]
    for (const material of materials) {
      const state = material?.userData?.[SCENE_MATERIAL_RENDERING_USER_DATA_KEY]
      if (!isRecord(state)) continue
      for (const key of ['visible', 'depthTest', 'depthWrite', 'colorWrite', 'toneMapped', 'wireframe', 'flatShading', 'alphaToCoverage', 'premultipliedAlpha', 'polygonOffset']) {
        if (typeof state[key] === 'boolean') material[key] = state[key]
      }
      for (const key of ['polygonOffsetFactor', 'polygonOffsetUnits', 'envMapIntensity']) {
        if (Number.isFinite(state[key])) material[key] = state[key]
      }
      if (typeof state.side === 'string' && THREE?.[state.side] !== undefined) material.side = THREE[state.side]
      material.needsUpdate = true
      applied += 1
    }
  })
  return applied
}
