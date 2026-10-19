import * as THREE from './vendor/three.module.js'
import { GLTFLoader } from './vendor/GLTFLoader.js'
import { OrbitControls } from './vendor/OrbitControls.js'

const host = document.querySelector('#scene')
const canvasRoot = document.querySelector('.viewport')
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
renderer.outputColorSpace = THREE.SRGBColorSpace
renderer.toneMapping = THREE.ACESFilmicToneMapping
renderer.toneMappingExposure = 1.25
renderer.shadowMap.enabled = true
renderer.shadowMap.type = THREE.PCFSoftShadowMap
host.appendChild(renderer.domElement)

const scene = new THREE.Scene()
scene.background = new THREE.Color('#10232a')
scene.fog = new THREE.Fog('#10232a', 22, 52)
const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 250)
camera.position.set(12, 9, 14)
scene.add(new THREE.HemisphereLight('#d1f8f1', '#15262c', 2.0))
const key = new THREE.DirectionalLight('#fff4dc', 4.2)
key.position.set(-8, 15, 9)
key.castShadow = true
key.shadow.mapSize.set(2048, 2048)
key.shadow.camera.left = -14
key.shadow.camera.right = 14
key.shadow.camera.top = 14
key.shadow.camera.bottom = -14
scene.add(key)
const fill = new THREE.PointLight('#16b8ad', 17, 28, 2)
fill.position.set(6, 7, -6)
scene.add(fill)

const controls = new OrbitControls(camera, renderer.domElement)
controls.target.set(0, 1.5, 0)
controls.enableDamping = true
controls.dampingFactor = 0.075
controls.maxPolarAngle = Math.PI * 0.48
controls.minDistance = 7
controls.maxDistance = 35

let warehouse = null
let disposed = false
let frame = 0
const bindings = []
let latest = { outputs: {}, time: null }
const allowedTargets = new Set(['position.x', 'position.y', 'position.z', 'rotation.x', 'rotation.y', 'rotation.z', 'visible', 'material.color'])

function updateMetric(id, value, digits = 1) {
  const element = document.getElementById(id)
  if (!element) return
  element.textContent = value === undefined || value === null || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(digits)
}

function applySample(sample) {
  latest = sample
  for (const { node, binding } of bindings) {
    const value = sample.outputs[binding.variable]
    if (value === undefined || value === null) continue
    if (binding.target === 'visible') { node.visible = Boolean(value); continue }
    if (binding.target === 'material.color') {
      if (node.material?.color) {
        node.material.color.set(value ? (binding.trueColor || '#ff2d55') : (binding.falseColor || '#24a875'))
        if (node.material.emissive) node.material.emissive.set(value ? '#46101c' : '#052b21')
      }
      continue
    }
    if (typeof value !== 'number' || !Number.isFinite(value)) continue
    const mapped = value * (Number(binding.scale) || 1) + (Number(binding.offset) || 0)
    if (Math.abs(mapped) > 100000) continue
    const [property, axis] = binding.target.split('.')
    node[property][axis] = mapped
  }
  updateMetric('orders', sample.outputs.ordersPicked, 0)
  updateMetric('inventory', sample.outputs.inventoryLevel, 0)
  updateMetric('position', sample.outputs.agvPosition, 2)
  const alarm = Boolean(sample.outputs.alarm)
  document.getElementById('alarm').textContent = alarm ? 'STOP · JAM' : 'SYSTEM READY'
  document.getElementById('alarm-card').classList.toggle('active', alarm)
  document.getElementById('time').textContent = `TIME ${Number.isFinite(sample.time) ? `${sample.time.toFixed(2)} s` : '—'}`
  document.getElementById('connection').textContent = Number.isFinite(sample.time) ? 'SIMULATION LINK ACTIVE' : 'WAITING FOR SIMULATION'
}

window.addEventListener('message', (event) => {
  if (event.source !== window.parent) return
  const data = event.data
  if (!data || data.type !== 'decentralabs:fmi-sample' || data.version !== 1) return
  if (data.outputs === null || typeof data.outputs !== 'object' || Array.isArray(data.outputs)) return
  const outputs = {}
  for (const [name, value] of Object.entries(data.outputs)) {
    if (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) outputs[name] = value
  }
  applySample({ time: Number.isFinite(data.time) ? data.time : null, outputs })
})

new GLTFLoader().load('./warehouse.glb', (gltf) => {
  if (disposed) return
  warehouse = gltf.scene
  const bounds = new THREE.Box3().setFromObject(warehouse)
  const center = bounds.getCenter(new THREE.Vector3())
  const size = bounds.getSize(new THREE.Vector3())
  const radius = Math.max(size.length() / 2, 1)
  camera.position.copy(center).add(new THREE.Vector3(radius * .9, radius * .66, radius * 1.05))
  camera.near = .05
  camera.far = radius * 10
  camera.lookAt(center)
  camera.updateProjectionMatrix()
  controls.target.copy(center)
  controls.minDistance = Math.max(radius * .28, 5)
  controls.maxDistance = radius * 3.4
  controls.update()
  warehouse.traverse((node) => {
    if (node.isMesh) { node.castShadow = true; node.receiveShadow = true }
    const declared = node.userData?.fmiBindings
    if (!Array.isArray(declared)) return
    for (const binding of declared) {
      if (binding && typeof binding.variable === 'string' && allowedTargets.has(binding.target)) bindings.push({ node, binding })
    }
  })
  scene.add(warehouse)
  document.getElementById('connection').textContent = 'SCENE READY · WAITING FOR FMI'
}, undefined, () => { document.getElementById('load-error').hidden = false })

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
  if (warehouse) applyBindingsOnly()
  controls.update()
  renderer.render(scene, camera)
}

function applyBindingsOnly() {
  for (const { node, binding } of bindings) {
    const value = latest.outputs[binding.variable]
    if (value === undefined || value === null) continue
    if (binding.target === 'visible') { node.visible = Boolean(value); continue }
    if (binding.target === 'material.color') {
      if (node.material?.color) node.material.color.set(value ? (binding.trueColor || '#ff2d55') : (binding.falseColor || '#24a875'))
      continue
    }
    if (typeof value !== 'number' || !Number.isFinite(value)) continue
    const mapped = value * (Number(binding.scale) || 1) + (Number(binding.offset) || 0)
    if (Math.abs(mapped) > 100000) continue
    const [property, axis] = binding.target.split('.')
    node[property][axis] = mapped
  }
}

render()
window.addEventListener('pagehide', () => {
  disposed = true
  cancelAnimationFrame(frame)
  resize.disconnect()
  controls.dispose()
  renderer.dispose()
})
