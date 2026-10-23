import * as THREE from '../webapp/vendor/three.module.js'
import { OrbitControls } from '../webapp/vendor/OrbitControls.js'
import { GLTFExporter } from '../webapp/vendor/GLTFExporter.js'
import { createSmartWarehouseScene, TRUCK_CONVEYOR_ENTRY_Z } from '../webapp/scene.js'

const status = document.getElementById('status')
const preview = document.getElementById('preview')
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6))
renderer.outputColorSpace = THREE.SRGBColorSpace
renderer.toneMapping = THREE.ACESFilmicToneMapping
renderer.toneMappingExposure = 0.94
renderer.shadowMap.enabled = true
renderer.shadowMap.type = THREE.PCFSoftShadowMap
preview.appendChild(renderer.domElement)

const scene = new THREE.Scene()
scene.background = new THREE.Color('#0a1c23')
const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 300)
const controls = new OrbitControls(camera, renderer.domElement)
controls.enableDamping = true
controls.dampingFactor = 0.075
controls.maxPolarAngle = Math.PI * 0.49

function binding(variable, target, options = {}) {
  return { variable, target, ...options }
}

function bind(node, bindings) {
  node.userData = { ...node.userData, fmiBindings: bindings }
}

function bindParcelDimensions(group) {
  const [body, tape, topTape, label] = group.children
  bind(body, [
    binding('packageSizeX', 'scale.x'),
    binding('packageSizeY', 'scale.y'),
    binding('packageSizeZ', 'scale.z'),
  ])
  bind(tape, [
    binding('packageSizeZ', 'position.z', { scale: 0.5, offset: 0.009 }),
    binding('packageSizeX', 'scale.x', { scale: 0.13 }),
    binding('packageSizeY', 'scale.y', { scale: 0.96 }),
  ])
  bind(topTape, [
    binding('packageSizeY', 'position.y', { scale: 0.5, offset: 0.006 }),
    binding('packageSizeX', 'scale.x', { scale: 0.13 }),
    binding('packageSizeZ', 'scale.z', { scale: 0.96 }),
  ])
  bind(label, [
    binding('packageSizeX', 'position.x', { scale: 0.18 }),
    binding('packageSizeY', 'position.y', { scale: -0.1 }),
    binding('packageSizeZ', 'position.z', { scale: 0.5, offset: 0.004 }),
    binding('packageSizeX', 'scale.x', { scale: 0.32 }),
    binding('packageSizeY', 'scale.y', { scale: 0.18 }),
  ])
}

function wrapColoredParcel(parcel, visibilityVariable) {
  const source = parcel.group
  const parent = source.parent
  const wrapper = new THREE.Group()
  wrapper.name = source.name
  wrapper.position.copy(source.position)
  wrapper.quaternion.copy(source.quaternion)
  wrapper.scale.copy(source.scale)
  bind(wrapper, [binding(visibilityVariable, 'visible')])

  for (const color of ['blue', 'gold', 'coral']) {
    const variant = source.clone(true)
    variant.name = `${source.name}_${color}`
    variant.position.set(0, 0, 0)
    variant.quaternion.identity()
    variant.scale.set(1, 1, 1)
    variant.visible = true
    bind(variant, [binding(`packageColor${color[0].toUpperCase()}${color.slice(1)}`, 'visible')])
    const body = variant.getObjectByName(`${source.name}_carton`)
    body.material = parcel.materials[color]
    bindParcelDimensions(variant)
    wrapper.add(variant)
  }

  parent.remove(source)
  parent.add(wrapper)
  return wrapper
}

function addFmiBindings(nodes) {
  nodes.sourcePackages.forEach((parcel, index) => {
    bind(parcel.group, [binding(`shelfPackage${index + 1}Visible`, 'visible')])
  })
  nodes.truckPackages.forEach((parcel, index) => {
    bind(parcel.group, [binding('truckLoadCount', 'visible', { visibleWhenGreaterThanOrEqual: index + 1 })])
  })

  bind(nodes.carrier, [binding('agvPosition', 'position.x', { offset: -6.2 })])
  bind(nodes.deck, [binding('forkHeight', 'position.y')])
  for (const wheel of nodes.carrierWheels) {
    bind(wheel.tire, [binding('agvPosition', 'rotation.y', { scale: -1 / 0.28 })])
  }
  for (const roller of nodes.deckRollers) {
    bind(roller, [binding('trayBeltPhase', 'rotation.y', { scale: -1 })])
  }
  for (const roller of nodes.conveyorRollers) {
    bind(roller, [binding('beltPhase', 'rotation.y', { scale: -1 })])
  }

  bind(nodes.pickup, [
    binding('pickupArmX', 'position.x'),
    binding('pickupArmY', 'position.y'),
    binding('pickupArmZ', 'position.z'),
    binding('pickupArmAngle', 'rotation.y'),
  ])
  bind(nodes.pickerReach, [
    binding('pickupArmReachProgress', 'visible', { visibleWhenGreaterThanOrEqual: 0.08 }),
    binding('pickupDropProgress', 'position.y'),
    binding('pickupArmReachProgress', 'position.z', { scale: 0.5 }),
    binding('pickupArmReachProgress', 'scale.z'),
  ])
  bind(nodes.pickerInnerReach, [
    binding('pickupArmReachProgress', 'visible', { visibleWhenGreaterThanOrEqual: 0.08 }),
    binding('pickupDropProgress', 'position.y'),
    binding('pickupArmReachProgress', 'position.z', { scale: 0.44, offset: 0.1 }),
    binding('pickupArmReachProgress', 'scale.z', { scale: 0.88 }),
  ])
  bind(nodes.pickerReachHead, [
    binding('pickupDropProgress', 'position.y'),
    binding('pickupArmReachProgress', 'position.z'),
  ])
  bind(nodes.pickerDropRod, [
    binding('pickupDropProgress', 'position.y', { scale: 0.5, offset: -0.15 }),
    binding('pickupDropProgress', 'scale.y', { scale: -1, offset: -0.299 }),
  ])
  bind(nodes.pickerDropInner, [
    binding('pickupDropProgress', 'position.y', { offset: -0.275 }),
    binding('pickupArmReachProgress', 'position.z'),
  ])
  bind(nodes.pickerTool, [
    binding('pickupDropProgress', 'position.y', { offset: -0.55 }),
    binding('pickupArmReachProgress', 'position.z'),
  ])
  bind(nodes.gripper, [binding('robotAngle', 'rotation.x')])
  bind(nodes.gripperLeft, [binding('pickupGripperGap', 'position.x', { scale: -0.5 })])
  bind(nodes.gripperRight, [binding('pickupGripperGap', 'position.x', { scale: 0.5 })])

  const pickupParcel = wrapColoredParcel(nodes.pickupParcel, 'pickupPackageVisible')
  const carrierParcel = wrapColoredParcel(nodes.carrierParcel, 'packageOnAgv')
  const transferParcel = wrapColoredParcel(nodes.transferParcel, 'transferPackageVisible')
  bind(carrierParcel, [
    binding('packageOnAgv', 'visible'),
    binding('packageSizeY', 'position.y', { scale: 0.5, offset: 0.13 }),
    binding('trayPackageX', 'position.x'),
  ])
  bind(transferParcel, [
    binding('transferPackageVisible', 'visible'),
    binding('transferPackageX', 'position.x'),
    binding('transferPackageY', 'position.y'),
    binding('transferPackageZ', 'position.z'),
  ])
  bind(pickupParcel, [binding('pickupPackageVisible', 'visible')])

  bind(nodes.loaderArm, [binding('loaderAngle', 'rotation.y')])
  bind(nodes.loaderReach, [
    binding('loaderReach', 'visible', { visibleWhenGreaterThanOrEqual: 0.04 }),
    binding('loaderReach', 'position.z', { scale: 0.5 }),
    binding('loaderReach', 'scale.z'),
  ])
  bind(nodes.loaderInnerReach, [
    binding('loaderReach', 'visible', { visibleWhenGreaterThanOrEqual: 0.04 }),
    binding('loaderReach', 'position.z', { scale: 0.43, offset: 0.08 }),
    binding('loaderReach', 'scale.z', { scale: 0.85 }),
  ])
  bind(nodes.loaderTool, [binding('loaderReach', 'position.z')])
  bind(nodes.alarmLens, [binding('alarm', 'material.color', { falseColor: '#31dfa0', trueColor: '#ff365f' })])
  bind(nodes.agvStatus, [binding('orderComplete', 'material.color', { falseColor: '#37e5cd', trueColor: '#50e0a4' })])
}

async function buildAndSaveGlb() {
  try {
    const built = createSmartWarehouseScene(scene, renderer)
    built.setDayMode(false)
    const initialOutputs = {
      truckLoadCount: 0,
      packageColorBlue: true,
      packageColorGold: false,
      packageColorCoral: false,
      packageSizeX: 0.48,
      packageSizeY: 0.42,
      packageSizeZ: 0.46,
      agvPosition: 0,
      forkHeight: 0.46,
      beltPhase: 0,
      trayPackageX: 0,
      trayBeltPhase: 0,
      pickupArmX: -6.2,
      pickupArmY: 6.4,
      pickupArmZ: -4.65,
      pickupArmReachProgress: 0,
      pickupDropProgress: -0.35,
      pickupGripperGap: 1,
      robotAngle: 0.12,
      pickupPackageVisible: false,
      packageOnAgv: false,
      transferPackageVisible: false,
      transferPackageX: 4.9,
      transferPackageY: 0.94,
      transferPackageZ: TRUCK_CONVEYOR_ENTRY_Z,
      loaderAngle: 0,
      loaderReach: 0,
      alarm: false,
      orderComplete: false,
    }
    built.update(initialOutputs)
    addFmiBindings(built.exportNodes)

    const direction = new THREE.Vector3(1.05, 0.74, 1.22).normalize()
    camera.position.copy(built.center).addScaledVector(direction, built.radius * 1.45)
    camera.near = 0.05
    camera.far = built.radius * 10
    camera.lookAt(built.center)
    camera.updateProjectionMatrix()
    controls.target.copy(built.center)
    controls.minDistance = Math.max(built.radius * 0.28, 5)
    controls.maxDistance = built.radius * 3.5
    controls.update()

    const resize = () => {
      const rect = preview.getBoundingClientRect()
      renderer.setSize(rect.width, rect.height, false)
      camera.aspect = Math.max(rect.width, 1) / Math.max(rect.height, 1)
      camera.updateProjectionMatrix()
    }
    new ResizeObserver(resize).observe(preview)
    resize()
    renderer.render(scene, camera)

    status.textContent = 'Exporting scene geometry, PBR materials, textures and FMI bindings…'
    const exporter = new GLTFExporter()
    const binary = await exporter.parseAsync(scene, { binary: true, onlyVisible: false, trs: true })
    const response = await fetch('/__export', {
      method: 'POST',
      headers: { 'Content-Type': 'model/gltf-binary' },
      body: binary,
    })
    if (!response.ok) throw new Error(`Local export receiver returned HTTP ${response.status}`)
    const result = await response.json()
    status.textContent = `Saved ${result.file} · ${Math.round(result.bytes / 1024)} KB · ${result.bindings} FMI-bound nodes`
  } catch (error) {
    status.textContent = `Export failed: ${error.message}`
    console.error(error)
  }
}

buildAndSaveGlb()
window.addEventListener('resize', () => {
  const rect = preview.getBoundingClientRect()
  renderer.setSize(rect.width, rect.height, false)
  camera.aspect = Math.max(rect.width, 1) / Math.max(rect.height, 1)
  camera.updateProjectionMatrix()
  renderer.render(scene, camera)
})
