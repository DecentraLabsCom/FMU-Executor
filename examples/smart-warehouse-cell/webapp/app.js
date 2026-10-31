import * as THREE from './vendor/three.module.js'
import { OrbitControls } from './vendor/OrbitControls.js'
import { RoomEnvironment } from './vendor/RoomEnvironment.js'
import { createSmartWarehouseScene } from './scene.js'
import { createDigitalTwinAppClient } from './sdk/client.js'

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

const controls = new OrbitControls(camera, renderer.domElement)
controls.target.set(0, 1.5, 0)
controls.enableDamping = true
controls.dampingFactor = 0.075
controls.maxPolarAngle = Math.PI * 0.49
controls.minDistance = 5
controls.maxDistance = 56

let warehouseScene = null
let disposed = false
let frame = 0
let sceneCenter = new THREE.Vector3()
let sceneRadius = 12
let theme = 'night'
let latest = { outputs: {}, time: null }
const dash = '\u2014'
let hostHandshakeReady = false
let sceneReady = false
const playback = {
  samples: [],
  sampleIndex: -1,
  playing: false,
  simulationRunning: false,
  autoPlayPending: true,
  modelTime: 0,
  lastFrameTime: 0,
  rate: 1,
}
const playbackButton = document.getElementById('playback-toggle')
const simulationRunButton = document.getElementById('run-simulation')
let lastSimulationState = 'idle'

function setText(id, value) {
  const element = document.getElementById(id)
  if (element) element.textContent = value
}

function formatted(value, digits = 1) {
  return value === undefined || value === null || !Number.isFinite(Number(value)) ? dash : Number(value).toFixed(digits)
}

function applySceneOutputs(outputs) {
  warehouseScene?.update(outputs)
}

function applySample(sample) {
  latest = sample
  applySceneOutputs(sample.outputs)
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

function updateSimulationControls() {
  if (simulationRunButton) {
    simulationRunButton.disabled = !hostHandshakeReady || !sceneReady || lastSimulationState === 'running'
    setText('run-simulation', lastSimulationState === 'running'
      ? 'SIMULATION RUNNING…'
      : lastSimulationState === 'completed' ? 'RUN AGAIN' : 'RUN SIMULATION')
  }
  const status = lastSimulationState === 'running'
    ? 'The FMU is calculating the response.'
    : lastSimulationState === 'completed'
      ? `${playback.samples.length.toLocaleString('en-US')} samples ready for replay.`
      : lastSimulationState === 'cancelled'
        ? 'Simulation cancelled. You can review the samples already received or start again.'
      : lastSimulationState === 'error'
        ? 'The FMU run failed. Adjust the inputs and try again.'
        : sceneReady ? '' : 'Preparing the warehouse scene…'
  setText('simulation-run-status', status)
}

function updateDurationLabel() {
  const duration = Number(document.getElementById('simulation-duration')?.value)
  setText('duration-value', Number.isFinite(duration) ? String(duration) : '250')
}

function requestSimulation() {
  if (!hostHandshakeReady || !sceneReady || lastSimulationState === 'running') return

  const speed = Number(document.getElementById('robot-speed')?.value)
  const quantity = Number(document.getElementById('order-quantity')?.value)
  const stopTime = Number(document.getElementById('simulation-duration')?.value)
  if (!Number.isFinite(speed) || !Number.isInteger(quantity) || !Number.isFinite(stopTime)) return

  lastSimulationState = 'running'
  updateSimulationControls()
  const settings = document.querySelector('.simulation-setup')
  if (settings) settings.open = false
  twinClient.run({
    batchPolicy: 'adaptive',
    parameters: {
      robotSpeed: Math.max(0, Math.min(1.4, speed)),
      orderQuantity: Math.max(1, Math.min(6, quantity)),
      startStop: Boolean(document.getElementById('start-stop')?.checked),
      jam: Boolean(document.getElementById('jam-input')?.checked),
      rushOrder: Boolean(document.getElementById('rush-order')?.checked),
    },
    options: { stopTime: Math.max(5, Math.min(300, stopTime)) },
  }).catch(() => {
    lastSimulationState = 'error'
    updateSimulationControls()
  })
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
  const elapsedSeconds = Math.max((now - playback.lastFrameTime) / 1000, 0)
  playback.lastFrameTime = now
  playback.modelTime += elapsedSeconds * playback.rate
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

function handleSimulationState(state) {
  playback.simulationRunning = state === 'running'
  lastSimulationState = ['running', 'completed', 'cancelled', 'error'].includes(state) ? state : 'idle'
  updateSimulationControls()
}

const twinClient = createDigitalTwinAppClient({
  onHostReady() {
    hostHandshakeReady = true
    updateSimulationControls()
  },
  onReset() {
    resetPlayback()
  },
  onSimulationState(state) {
    if (state !== 'completed') handleSimulationState(state)
  },
  onResultsComplete({ state }) {
    handleSimulationState(state)

  },
  onPlaybackRate(rate, message) {
    if (
      message.rateUnit === 'model-seconds-per-real-second'
      && Number.isFinite(Number(rate))
      && Number(rate) > 0
      && Number(rate) >= 0.01
      && Number(rate) <= 100000
    ) playback.rate = Number(rate)
  },
  onSamples(samples) {
    appendSamples(samples)
  },
  onBatchRendered() {
    return new Promise((resolve) => {
      let frameId = 0
      const timeoutId = window.setTimeout(() => {
        if (frameId) cancelAnimationFrame(frameId)
        resolve()
      }, 100)
      frameId = requestAnimationFrame(() => {
        window.clearTimeout(timeoutId)
        resolve()
      })
    })
  },
  onSample(sample) {
    if (!sample.outputs || typeof sample.outputs !== 'object' || Array.isArray(sample.outputs)) return
    const outputs = {}
    for (const [name, value] of Object.entries(sample.outputs)) {
      if (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) outputs[name] = value
    }
    applySample({ time: Number.isFinite(sample.time) ? sample.time : null, outputs })
  },
  onFmuStateLoaded(sample) {
    if (!sample?.outputs || typeof sample.outputs !== 'object' || Array.isArray(sample.outputs)
      || !Number.isFinite(sample.time)) return

    const loadedSample = {
      time: sample.time,
      outputs: Object.fromEntries(Object.entries(sample.outputs).filter(([, value]) => (
        typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))
      ))),
    }
    setPlaybackPlaying(false)
    playback.simulationRunning = false
    playback.autoPlayPending = false
    let sampleIndex = playback.samples.findIndex((entry) => Math.abs(entry.time - loadedSample.time) <= 1e-12)
    if (sampleIndex >= 0) {
      playback.samples[sampleIndex] = loadedSample
    } else {
      sampleIndex = playback.samples.findIndex((entry) => entry.time > loadedSample.time)
      if (sampleIndex < 0) sampleIndex = playback.samples.length
      playback.samples.splice(sampleIndex, 0, loadedSample)
    }
    playback.sampleIndex = sampleIndex
    playback.modelTime = loadedSample.time
    applySample(loadedSample)
    updatePlaybackButton()
  },
  onError(error) {
    console.error('Digital twin app bridge error.', error)
  },
  playbackControlPosition: 'bottom-right',
})

playbackButton?.addEventListener('click', togglePlayback)
simulationRunButton?.addEventListener('click', requestSimulation)
document.getElementById('simulation-duration')?.addEventListener('input', updateDurationLabel)
updateDurationLabel()
updatePlaybackButton()
updateSimulationControls()

try {
  warehouseScene = createSmartWarehouseScene(scene, renderer)
  sceneCenter = warehouseScene.center
  sceneRadius = warehouseScene.radius
  const direction = new THREE.Vector3(1.05, 0.74, 1.22).normalize()
  camera.position.copy(sceneCenter).addScaledVector(direction, sceneRadius * 1.45)
  camera.near = 0.05
  camera.far = sceneRadius * 10
  camera.lookAt(sceneCenter)
  camera.updateProjectionMatrix()
  controls.target.copy(sceneCenter)
  controls.minDistance = Math.max(sceneRadius * 0.28, 5)
  controls.maxDistance = sceneRadius * 3.5
  controls.update()
  applySceneOutputs(latest.outputs)
  setText('connection', 'PROCEDURAL SCENE READY - WAITING FOR FMI')
  sceneReady = true
  updateSimulationControls()
  twinClient.reportReady()
} catch (error) {
  document.getElementById('load-error').hidden = false
  twinClient.reportError('SCENE_INIT_FAILED')
  console.error('Could not build the procedural Smart Warehouse scene.', error)
}

function setCamera(mode) {
  const direction = mode === 'top'
    ? new THREE.Vector3(.015, 1, .015)
    : new THREE.Vector3(1.05, .74, 1.22).normalize()
  const distance = mode === 'top' ? sceneRadius * 1.9 : sceneRadius * 1.45
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
  warehouseScene?.setDayMode(day)
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
  const now = performance.now()
  advancePlayback(now)
  warehouseScene?.animate(now / 1000)
  controls.update()
  renderer.render(scene, camera)
}

render()
window.addEventListener('pagehide', () => {
  disposed = true
  cancelAnimationFrame(frame)
  resize.disconnect()
  controls.dispose()
  warehouseScene?.dispose()
  environmentTarget.dispose()
  pmremGenerator.dispose()
  renderer.dispose()
})
