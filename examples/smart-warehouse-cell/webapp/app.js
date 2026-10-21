import * as THREE from './vendor/three.module.js'
import { GLTFLoader } from './vendor/GLTFLoader.js'
import { OrbitControls } from './vendor/OrbitControls.js'
import { RoomEnvironment } from './vendor/RoomEnvironment.js'

const host = document.querySelector('#scene')
const canvasRoot = document.querySelector('.viewport')
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' })
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75))
renderer.outputColorSpace = THREE.SRGBColorSpace
renderer.toneMapping = THREE.ACESFilmicToneMapping
renderer.toneMappingExposure = 0.94
renderer.shadowMap.enabled = true
renderer.shadowMap.type = THREE.PCFSoftShadowMap
host.appendChild(renderer.domElement)

const scene = new THREE.Scene()
scene.background = new THREE.Color('#0a1c23')
scene.fog = new THREE.Fog('#0a1c23', 34, 92)
const pmremGenerator = new THREE.PMREMGenerator(renderer)
const studioEnvironment = new RoomEnvironment()
const environmentTarget = pmremGenerator.fromScene(studioEnvironment, 0.04)
scene.environment = environmentTarget.texture
studioEnvironment.dispose()
const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 300)
const hemisphere = new THREE.HemisphereLight('#d4fff1', '#18242a', 1.2)
scene.add(hemisphere)
const key = new THREE.DirectionalLight('#fff1d4', 2.35)
key.position.set(-10, 18, 12)
key.castShadow = true
key.shadow.mapSize.set(2048, 2048)
key.shadow.camera.left = -20
key.shadow.camera.right = 20
key.shadow.camera.top = 20
key.shadow.camera.bottom = -20
scene.add(key)
const fill = new THREE.PointLight('#12c9bb', 10, 38, 2)
fill.position.set(6, 8, -7)
scene.add(fill)
const rim = new THREE.DirectionalLight('#40b6dc', 0.9)
rim.position.set(10, 10, -12)
scene.add(rim)
const roofLights = []
for (const [index, z] of [-5, -1.7, 1.7, 5].entries()) {
  const roofLight = new THREE.PointLight(index % 2 ? '#c8f4ff' : '#ffe4bc', 8, 15, 2)
  roofLight.position.set(index % 2 ? 3.4 : -3.4, 6.0, z)
  scene.add(roofLight)
  roofLights.push(roofLight)
}

const controls = new OrbitControls(camera, renderer.domElement)
controls.target.set(0, 1.5, 0)
controls.enableDamping = true
controls.dampingFactor = 0.075
controls.maxPolarAngle = Math.PI * 0.49
controls.minDistance = 5
controls.maxDistance = 56

let warehouse = null
let disposed = false
let frame = 0
let sceneCenter = new THREE.Vector3()
let sceneRadius = 12
let theme = 'night'
const bindings = []
let latest = { outputs: {}, time: null }
const allowedTargets = new Set(['position.x', 'position.y', 'position.z', 'rotation.x', 'rotation.y', 'rotation.z', 'scale.x', 'scale.y', 'scale.z', 'visible', 'material.color'])
const dash = '\u2014'
let hostHandshakeReady = false
let sceneReady = false
let sceneErrorCode = null
const playback = {
  samples: [],
  sampleIndex: -1,
  playing: false,
  simulationRunning: false,
  autoPlayPending: true,
  modelTime: 0,
  lastFrameTime: 0,
}
const PLAYBACK_SPEED = 10
const playbackButton = document.getElementById('playback-toggle')

function setText(id, value) {
  const element = document.getElementById(id)
  if (element) element.textContent = value
}

function formatted(value, digits = 1) {
  return value === undefined || value === null || !Number.isFinite(Number(value)) ? dash : Number(value).toFixed(digits)
}

function applyBindings(outputs) {
  for (const { node, binding } of bindings) {
    const value = outputs[binding.variable]
    if (value === undefined || value === null) continue
    if (binding.target === 'visible') {
      if (Object.prototype.hasOwnProperty.call(binding, 'visibleWhenGreaterThanOrEqual')) {
        const threshold = Number(binding.visibleWhenGreaterThanOrEqual)
        const numeric = Number(value)
        node.visible = Number.isFinite(threshold) && Number.isFinite(numeric) && numeric >= threshold
      } else {
        node.visible = Boolean(value)
      }
      continue
    }
    if (binding.target === 'material.color') {
      if (node.material?.color) {
        node.material.color.set(value ? (binding.trueColor || '#ff365f') : (binding.falseColor || '#31dfa0'))
        if (node.material.emissive) node.material.emissive.set(value ? '#51101f' : '#06382a')
      }
      continue
    }
    if (typeof value !== 'number' || !Number.isFinite(value)) continue
    const mapped = value * (Number(binding.scale) || 1) + (Number(binding.offset) || 0)
    if (Math.abs(mapped) > 100000) continue
    const [property, axis] = binding.target.split('.')
    node[property][axis] = mapped
  }
}

function applySample(sample) {
  latest = sample
  applyBindings(sample.outputs)
  setText('orders', formatted(sample.outputs.ordersPicked, 0))
  setText('truck-load', `${formatted(sample.outputs.truckLoadCount, 0)} / 6`)
  setText('inventory', formatted(sample.outputs.inventoryLevel, 0))
  setText('position', formatted(sample.outputs.agvPosition, 2))
  setText('fork-height', formatted(sample.outputs.forkHeight, 2))
  setText('robot-angle', formatted(sample.outputs.robotAngle, 2))

  const inventory = Number(sample.outputs.inventoryLevel)
  document.getElementById('inventory-meter').style.width = Number.isFinite(inventory)
    ? `${Math.max(0, Math.min(100, inventory))}%`
    : '0%'
  const truckLoad = Number(sample.outputs.truckLoadCount)
  document.getElementById('truck-load-meter').style.width = Number.isFinite(truckLoad)
    ? `${Math.max(0, Math.min(6, truckLoad)) / 6 * 100}%`
    : '0%'

  const packageOnAgv = Boolean(sample.outputs.packageOnAgv)
  const packageState = document.querySelector('.package-state')
  const transferVisible = Boolean(sample.outputs.transferPackageVisible)
  const pickupVisible = Boolean(sample.outputs.pickupPackageVisible)
  const pickupProgress = Number(sample.outputs.pickupProgress)
  const pickupActive = pickupVisible || (Number.isFinite(pickupProgress) && pickupProgress > 0 && pickupProgress < 1)
  const orderComplete = Boolean(sample.outputs.orderComplete)
  const agvPosition = Number(sample.outputs.agvPosition)
  const phase = orderComplete
    ? 'ORDER COMPLETE'
    : transferVisible
      ? 'LOADING TRUCK'
      : packageOnAgv
        ? 'AGV TO TRUCK'
        : pickupActive
          ? 'PICKING FROM RACK'
          : agvPosition > 0.05
            ? 'AGV RETURNING'
            : 'WAITING FOR PICK'
  packageState.classList.toggle('active', packageOnAgv || transferVisible || pickupActive)
  packageState.classList.toggle('complete', orderComplete)
  setText('package-state', phase)

  const alarm = Boolean(sample.outputs.alarm)
  document.getElementById('alarm-card').classList.toggle('active', alarm)
  setText('alarm', alarm ? 'JAM DETECTED' : 'SYSTEM READY')
  setText('time', `t ${formatted(sample.time, 2)} s`)
  setText('connection', Number.isFinite(sample.time) ? 'SIMULATION LINK ACTIVE' : 'WAITING FOR SIMULATION')
}

function updatePlaybackButton() {
  if (!playbackButton) return
  const canPlay = playback.samples.length > 1
  playbackButton.disabled = !canPlay
  playbackButton.setAttribute('aria-pressed', String(playback.playing))
  playbackButton.setAttribute('aria-label', playback.playing ? 'Pause simulation replay' : 'Play simulation replay')
  setText('playback-label', playback.playing ? 'PAUSE' : 'PLAY')
  const icon = playbackButton.querySelector('.playback-icon')
  if (icon) icon.textContent = playback.playing ? '\u275a\u275a' : '\u25b6'
}

function setPlaybackPlaying(playing) {
  playback.playing = playing
  playback.lastFrameTime = 0
  updatePlaybackButton()
}

function startPlayback() {
  if (playback.samples.length < 2) return
  if (playback.sampleIndex < 0 || (playback.sampleIndex >= playback.samples.length - 1 && !playback.simulationRunning)) {
    playback.sampleIndex = 0
  }
  playback.modelTime = playback.samples[playback.sampleIndex].time
  applySample(playback.samples[playback.sampleIndex])
  setPlaybackPlaying(true)
}

function togglePlayback() {
  if (playback.playing) {
    setPlaybackPlaying(false)
    return
  }
  startPlayback()
}

function resetPlayback() {
  playback.samples = []
  playback.sampleIndex = -1
  playback.modelTime = 0
  playback.simulationRunning = false
  playback.autoPlayPending = true
  setPlaybackPlaying(false)
}

function appendSamples(rawSamples) {
  if (!Array.isArray(rawSamples)) return
  const wasEmpty = playback.samples.length === 0
  for (const rawSample of rawSamples) {
    if (!rawSample || typeof rawSample !== 'object' || !Number.isFinite(rawSample.time)) continue
    if (playback.samples.length > 0 && rawSample.time <= playback.samples[playback.samples.length - 1].time) continue
    if (!rawSample.outputs || typeof rawSample.outputs !== 'object' || Array.isArray(rawSample.outputs)) continue
    const outputs = {}
    for (const [name, value] of Object.entries(rawSample.outputs)) {
      if (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) outputs[name] = value
    }
    playback.samples.push({ time: rawSample.time, outputs })
  }
  if (wasEmpty && playback.samples.length > 0) {
    playback.sampleIndex = 0
    playback.modelTime = playback.samples[0].time
    applySample(playback.samples[0])
  }
  if (playback.autoPlayPending && playback.samples.length > 1) {
    playback.autoPlayPending = false
    startPlayback()
  }
  updatePlaybackButton()
}

function advancePlayback(now) {
  if (!playback.playing || playback.sampleIndex < 0) return
  if (playback.lastFrameTime === 0) {
    playback.lastFrameTime = now
    return
  }
  const elapsedSeconds = Math.min((now - playback.lastFrameTime) / 1000, 0.1)
  playback.lastFrameTime = now
  playback.modelTime += elapsedSeconds * PLAYBACK_SPEED
  const lastSample = playback.samples[playback.samples.length - 1]
  if (playback.simulationRunning && lastSample) playback.modelTime = Math.min(playback.modelTime, lastSample.time)

  let nextIndex = playback.sampleIndex
  while (nextIndex + 1 < playback.samples.length && playback.samples[nextIndex + 1].time <= playback.modelTime) {
    nextIndex += 1
  }
  if (nextIndex !== playback.sampleIndex) {
    playback.sampleIndex = nextIndex
    applySample(playback.samples[nextIndex])
  }
  if (!playback.simulationRunning && playback.sampleIndex >= playback.samples.length - 1) {
    setPlaybackPlaying(false)
  }
}

function reportSceneStatus() {
  if (!hostHandshakeReady || window.parent === window) return
  if (sceneErrorCode) {
    window.parent.postMessage({
      type: 'decentralabs:digital-twin-error',
      version: 1,
      code: sceneErrorCode,
    }, '*')
  } else if (sceneReady) {
    window.parent.postMessage({
      type: 'decentralabs:digital-twin-ready',
      version: 1,
    }, '*')
  }
}

window.addEventListener('message', (event) => {
  if (event.source !== window.parent) return
  const data = event.data
  if (data?.type === 'decentralabs:digital-twin-host-ready' && data.version === 1) {
    hostHandshakeReady = true
    reportSceneStatus()
    return
  }
  if (!data || data.version !== 1) return
  if (data.type === 'decentralabs:fmi-reset') {
    resetPlayback()
    return
  }
  if (data.type === 'decentralabs:fmi-simulation-state') {
    playback.simulationRunning = data.state === 'running'
    return
  }
  if (data.type === 'decentralabs:fmi-samples') {
    appendSamples(data.samples)
    return
  }
  if (data.type === 'decentralabs:fmi-sample' && data.outputs && typeof data.outputs === 'object' && !Array.isArray(data.outputs)) {
    const outputs = {}
    for (const [name, value] of Object.entries(data.outputs)) {
      if (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) outputs[name] = value
    }
    applySample({ time: Number.isFinite(data.time) ? data.time : null, outputs })
  }
})

playbackButton?.addEventListener('click', togglePlayback)
updatePlaybackButton()

new GLTFLoader().load('./warehouse.glb', (gltf) => {
  if (disposed) return
  warehouse = gltf.scene
  const bounds = new THREE.Box3().setFromObject(warehouse)
  sceneCenter = bounds.getCenter(new THREE.Vector3())
  sceneRadius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 1)
  const direction = new THREE.Vector3(1.05, .74, 1.22).normalize()
  camera.position.copy(sceneCenter).addScaledVector(direction, sceneRadius * 1.72)
  camera.near = .05
  camera.far = sceneRadius * 10
  camera.lookAt(sceneCenter)
  camera.updateProjectionMatrix()
  controls.target.copy(sceneCenter)
  controls.minDistance = Math.max(sceneRadius * .28, 5)
  controls.maxDistance = sceneRadius * 3.5
  controls.update()
  warehouse.traverse((node) => {
    if (node.isMesh) {
      node.castShadow = true
      node.receiveShadow = true
      if (node.material) node.material.envMapIntensity = 0.48
    }
    const declared = node.userData?.fmiBindings
    if (!Array.isArray(declared)) return
    for (const binding of declared) {
      if (binding && typeof binding.variable === 'string' && allowedTargets.has(binding.target)) bindings.push({ node, binding })
    }
  })
  scene.add(warehouse)
  applyBindings(latest.outputs)
  setText('connection', 'SCENE READY · WAITING FOR FMI')
  sceneReady = true
  reportSceneStatus()
}, undefined, () => {
  sceneErrorCode = 'SCENE_LOAD_FAILED'
  document.getElementById('load-error').hidden = false
  reportSceneStatus()
})

function setCamera(mode) {
  const direction = mode === 'top'
    ? new THREE.Vector3(.015, 1, .015)
    : new THREE.Vector3(1.05, .74, 1.22).normalize()
  const distance = mode === 'top' ? sceneRadius * 2.1 : sceneRadius * 1.72
  camera.position.copy(sceneCenter).addScaledVector(direction, distance)
  controls.target.copy(sceneCenter)
  controls.update()
}

document.querySelectorAll('[data-view]').forEach((button) => {
  button.addEventListener('click', () => setCamera(button.dataset.view === 'top' ? 'top' : 'reset'))
})

document.getElementById('theme-toggle').addEventListener('click', (event) => {
  theme = theme === 'night' ? 'day' : 'night'
  const day = theme === 'day'
  const background = new THREE.Color(day ? '#8ba8a5' : '#0a1c23')
  scene.background = background
  scene.fog.color.copy(background)
  scene.fog.near = day ? 48 : 34
  scene.fog.far = day ? 125 : 92
  hemisphere.intensity = day ? 1.55 : 1.2
  key.intensity = day ? 2.8 : 2.35
  fill.intensity = day ? 6 : 10
  roofLights.forEach((light) => { light.intensity = day ? 5 : 8 })
  document.body.dataset.theme = theme
  event.currentTarget.textContent = day ? 'NIGHT MODE' : 'DAY MODE'
})

const resize = new ResizeObserver(() => {
  const width = canvasRoot.clientWidth
  const height = canvasRoot.clientHeight
  if (!width || !height) return
  camera.aspect = width / height
  camera.updateProjectionMatrix()
  renderer.setSize(width, height, false)
})
resize.observe(canvasRoot)

function render() {
  if (disposed) return
  frame = requestAnimationFrame(render)
  advancePlayback(performance.now())
  controls.update()
  renderer.render(scene, camera)
}

render()
window.addEventListener('pagehide', () => {
  disposed = true
  cancelAnimationFrame(frame)
  resize.disconnect()
  controls.dispose()
  environmentTarget.dispose()
  pmremGenerator.dispose()
  renderer.dispose()
})
