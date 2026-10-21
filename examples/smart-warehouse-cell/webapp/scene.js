import * as THREE from './vendor/three.module.js'

const SOURCE_PACKAGE_X = [-4.0, -3.5, -3.0, -1.7, -1.2, -0.7]
const PACKAGE_SIZES = [
  [0.48, 0.42, 0.46],
  [0.60, 0.54, 0.40],
  [0.42, 0.36, 0.64],
  [0.56, 0.60, 0.50],
  [0.60, 0.48, 0.62],
  [0.50, 0.56, 0.58],
]
const PACKAGE_PALETTE = ['blue', 'gold', 'coral']
const CARGO_SLOTS = [
  [0.0, 7.27], [-0.7, 7.27], [0.7, 7.27],
  [0.0, 6.27], [-0.7, 6.27], [0.7, 6.27],
]
const UP = new THREE.Vector3(0, 1, 0)

function seededNoiseTexture(base, seed, size = 256) {
  const canvas = document.createElement('canvas')
  canvas.width = size
  canvas.height = size
  const context = canvas.getContext('2d')
  const image = context.createImageData(size, size)
  let value = seed >>> 0
  for (let pixel = 0; pixel < size * size; pixel += 1) {
    value = (Math.imul(value, 1664525) + 1013904223) >>> 0
    const grain = ((value >>> 24) - 128) * 0.16
    const offset = pixel * 4
    image.data[offset] = Math.max(0, Math.min(255, base[0] + grain))
    image.data[offset + 1] = Math.max(0, Math.min(255, base[1] + grain))
    image.data[offset + 2] = Math.max(0, Math.min(255, base[2] + grain))
    image.data[offset + 3] = 255
  }
  context.putImageData(image, 0, 0)
  return canvas
}

function makeRoundedBoxGeometry(width = 1, height = 1, depth = 1, radius = 0.08) {
  const bevel = Math.min(radius, width * 0.18, height * 0.18, depth * 0.18)
  const halfWidth = width / 2 - bevel
  const halfDepth = depth / 2 - bevel
  const corner = Math.min(bevel, halfWidth * 0.45, halfDepth * 0.45)
  const shape = new THREE.Shape()
  shape.moveTo(-halfWidth + corner, -halfDepth)
  shape.lineTo(halfWidth - corner, -halfDepth)
  shape.quadraticCurveTo(halfWidth, -halfDepth, halfWidth, -halfDepth + corner)
  shape.lineTo(halfWidth, halfDepth - corner)
  shape.quadraticCurveTo(halfWidth, halfDepth, halfWidth - corner, halfDepth)
  shape.lineTo(-halfWidth + corner, halfDepth)
  shape.quadraticCurveTo(-halfWidth, halfDepth, -halfWidth, halfDepth - corner)
  shape.lineTo(-halfWidth, -halfDepth + corner)
  shape.quadraticCurveTo(-halfWidth, -halfDepth, -halfWidth + corner, -halfDepth)

  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth: height - bevel * 2,
    steps: 1,
    bevelEnabled: true,
    bevelSegments: 3,
    bevelSize: bevel,
    bevelThickness: bevel,
    curveSegments: 5,
  })
  geometry.rotateX(-Math.PI / 2)
  geometry.computeBoundingBox()
  const center = geometry.boundingBox.getCenter(new THREE.Vector3())
  geometry.translate(-center.x, -center.y, -center.z)
  geometry.computeVertexNormals()
  return geometry
}

function createSignTexture(renderer) {
  const canvas = document.createElement('canvas')
  canvas.width = 1024
  canvas.height = 256
  const context = canvas.getContext('2d')
  const gradient = context.createLinearGradient(0, 0, canvas.width, canvas.height)
  gradient.addColorStop(0, '#102633')
  gradient.addColorStop(1, '#1c4952')
  context.fillStyle = gradient
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.fillStyle = '#33d9c3'
  context.fillRect(0, 0, 14, canvas.height)
  context.fillStyle = '#ffc56f'
  context.fillRect(34, 34, 190, 8)
  context.font = '700 35px Arial, sans-serif'
  context.fillStyle = '#8caeb4'
  context.fillText('DECENTRALABS  /  LOGISTICS', 42, 92)
  context.font = '800 76px Arial, sans-serif'
  context.fillStyle = '#f0f6f2'
  context.fillText('FULFILLMENT HUB 04', 42, 178)
  context.font = '600 27px Arial, sans-serif'
  context.fillStyle = '#71e3d1'
  context.fillText('SMART WAREHOUSE  ·  LIVE FMI CELL', 42, 222)
  context.strokeStyle = 'rgba(170,230,221,.18)'
  context.lineWidth = 2
  for (let x = 260; x < 1000; x += 32) {
    context.beginPath()
    context.moveTo(x, 18)
    context.lineTo(x, 25)
    context.stroke()
  }
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8)
  return texture
}

function createShippingLabelTexture(renderer) {
  const canvas = document.createElement('canvas')
  canvas.width = 256
  canvas.height = 128
  const context = canvas.getContext('2d')
  context.fillStyle = '#f2ecde'
  context.fillRect(0, 0, 256, 128)
  context.fillStyle = '#183845'
  context.font = '700 20px Arial, sans-serif'
  context.fillText('DL  /  SMART', 14, 29)
  context.font = '600 14px Arial, sans-serif'
  context.fillStyle = '#547078'
  context.fillText('FRAGILE  ·  FMI', 14, 49)
  let x = 14
  for (let index = 0; index < 48; index += 1) {
    const barWidth = (index * 17 + 7) % 5 < 2 ? 2 : 1
    context.fillStyle = index % 7 === 0 ? '#168578' : '#17252b'
    context.fillRect(x, 66, barWidth, 46)
    x += barWidth + ((index * 5) % 3 === 0 ? 2 : 1)
    if (x > 242) break
  }
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8)
  return texture
}

function makeParcelMaterials(renderer, textures) {
  const palettes = {
    blue: ['#1669cc', '#32c8dd'],
    gold: ['#d88a24', '#ffd268'],
    coral: ['#d64d47', '#ff8a69'],
  }
  const materials = {}
  for (const [name, [base, accent]] of Object.entries(palettes)) {
    const hex = base.slice(1)
    const texture = new THREE.CanvasTexture(seededNoiseTexture([
      Number.parseInt(hex.slice(0, 2), 16),
      Number.parseInt(hex.slice(2, 4), 16),
      Number.parseInt(hex.slice(4, 6), 16),
    ], name.length * 911 + base.length))
    texture.colorSpace = THREE.SRGBColorSpace
    texture.wrapS = THREE.RepeatWrapping
    texture.wrapT = THREE.RepeatWrapping
    texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8)
    textures.push(texture)
    materials[name] = new THREE.MeshStandardMaterial({
      map: texture,
      color: '#ffffff',
      roughness: 0.72,
      metalness: 0.02,
    })
    materials[`${name}Accent`] = new THREE.MeshStandardMaterial({
      color: accent,
      roughness: 0.44,
      metalness: 0.08,
    })
  }
  return materials
}

function material(color, roughness = 0.5, metalness = 0.1, extra = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness, metalness, ...extra })
}

function addMesh(parent, geometry, mat, position, scale = [1, 1, 1], options = {}) {
  const mesh = new THREE.Mesh(geometry, mat)
  mesh.position.set(position[0], position[1], position[2])
  mesh.scale.set(scale[0], scale[1], scale[2])
  mesh.castShadow = options.castShadow ?? true
  mesh.receiveShadow = options.receiveShadow ?? true
  if (options.rotation) mesh.rotation.set(...options.rotation)
  if (options.name) mesh.name = options.name
  parent.add(mesh)
  return mesh
}

function addBar(parent, geometry, mat, start, end, radius, options = {}) {
  const direction = new THREE.Vector3(end[0] - start[0], end[1] - start[1], end[2] - start[2])
  const length = direction.length()
  const center = new THREE.Vector3(
    (start[0] + end[0]) / 2,
    (start[1] + end[1]) / 2,
    (start[2] + end[2]) / 2,
  )
  const mesh = new THREE.Mesh(geometry, mat)
  mesh.position.copy(center)
  mesh.quaternion.setFromUnitVectors(UP, direction.normalize())
  mesh.scale.set(radius, length, radius)
  mesh.castShadow = options.castShadow ?? true
  mesh.receiveShadow = options.receiveShadow ?? true
  parent.add(mesh)
  return mesh
}

function setParcelDimensions(parcel, size, color) {
  const [width, height, depth] = size
  parcel.group.scale.set(1, 1, 1)
  parcel.body.scale.set(width, height, depth)
  parcel.body.material = parcel.materials[color] || parcel.materials.blue
  parcel.tape.scale.set(width * 0.13, height * 0.96, 0.012)
  parcel.tape.position.set(0, 0, depth / 2 + 0.009)
  parcel.topTape.scale.set(width * 0.13, 0.012, depth * 0.96)
  parcel.topTape.position.set(0, height / 2 + 0.006, 0)
  parcel.label.scale.set(width * 0.43, height * 0.25, 1)
  parcel.label.position.set(width * 0.20, -height * 0.12, depth / 2 + 0.018)
}

function makeParcel(parent, geometry, boxGeometry, materials, labelMaterial, position, size, color, name) {
  const group = new THREE.Group()
  group.position.set(position[0], position[1], position[2])
  group.name = name
  const body = addMesh(group, geometry, materials[color] || materials.blue, [0, 0, 0], size, { name: `${name}_carton` })
  const tape = addMesh(group, boxGeometry, materials.tape, [0, 0, 0], [0.1, 0.9, 0.01], { castShadow: false })
  const topTape = addMesh(group, boxGeometry, materials.tape, [0, 0, 0], [0.1, 0.01, 0.9], { castShadow: false })
  const label = addMesh(group, boxGeometry, labelMaterial, [0, 0, 0], [0.4, 0.25, 0.01], { castShadow: false })
  parent.add(group)
  const parcel = { group, body, tape, topTape, label, materials }
  setParcelDimensions(parcel, size, color)
  return parcel
}

function makeCanvasTexture(renderer, canvas) {
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8)
  return texture
}

export function createSmartWarehouseScene(scene, renderer) {
  const root = new THREE.Group()
  root.name = 'SmartWarehouseProceduralScene'
  scene.add(root)

  const boxGeometry = new THREE.BoxGeometry(1, 1, 1)
  const roundedBoxGeometry = makeRoundedBoxGeometry()
  const cylinderGeometry = new THREE.CylinderGeometry(1, 1, 1, 32, 1)
  const sphereGeometry = new THREE.SphereGeometry(1, 28, 20)
  const torusGeometry = new THREE.TorusGeometry(1, 0.16, 12, 36)
  const wheelGeometry = new THREE.CylinderGeometry(0.34, 0.34, 0.22, 40, 1)
  const rollerGeometry = new THREE.CylinderGeometry(0.055, 0.055, 0.92, 24, 1)
  const textures = []
  const floorCanvas = seededNoiseTexture([104, 117, 119], 24017)
  const floorTexture = makeCanvasTexture(renderer, floorCanvas)
  floorTexture.wrapS = THREE.RepeatWrapping
  floorTexture.wrapT = THREE.RepeatWrapping
  floorTexture.repeat.set(18, 15)
  textures.push(floorTexture)
  const signTexture = createSignTexture(renderer)
  const labelTexture = createShippingLabelTexture(renderer)
  textures.push(signTexture, labelTexture)

  const parcelMaterials = makeParcelMaterials(renderer, textures)
  const labelMaterial = material('#ffffff', 0.72, 0, { map: labelTexture, side: THREE.DoubleSide })
  const materials = {
    foundation: material('#17242b', 0.82, 0.18),
    concrete: material('#b4c0bc', 0.82, 0.04, { map: floorTexture }),
    apron: material('#515d61', 0.88, 0.02),
    aisle: material('#182d35', 0.78, 0.18),
    marking: material('#f1b83d', 0.46, 0.2),
    safe: material('#37d8c2', 0.35, 0.22, { emissive: '#087e72', emissiveIntensity: 1.2 }),
    darkSteel: material('#243941', 0.35, 0.76),
    metal: material('#9aaeb0', 0.29, 0.76),
    brightMetal: material('#d2ddda', 0.24, 0.68),
    graphite: material('#15262d', 0.34, 0.63),
    rackUpright: material('#136277', 0.36, 0.58),
    rackOrange: material('#ed873b', 0.38, 0.46),
    rackDeck: material('#34474a', 0.52, 0.62),
    warehouseWall: material('#344b54', 0.76, 0.28),
    wallCladding: material('#405a63', 0.68, 0.36),
    wallRib: material('#58737a', 0.42, 0.62),
    wallGlass: material('#123744', 0.22, 0.28, { transparent: true, opacity: 0.76, emissive: '#071b22', emissiveIntensity: 0.28 }),
    dockFrame: material('#e88a36', 0.42, 0.32),
    wood: material('#9c7045', 0.84, 0.02),
    rubber: material('#12191c', 0.94, 0.01),
    tireSidewall: material('#252d30', 0.82, 0.02),
    bluePaint: material('#12699b', 0.31, 0.46),
    blueDark: material('#0e384d', 0.38, 0.45),
    whitePaint: material('#d9e1dd', 0.32, 0.3),
    glass: material('#103541', 0.16, 0.58, { transparent: true, opacity: 0.88 }),
    goldPaint: material('#f0a833', 0.32, 0.42),
    redPaint: material('#d84f45', 0.34, 0.36),
    tireHub: material('#83a0a3', 0.26, 0.82),
    belt: material('#152329', 0.83, 0.12),
    glow: material('#37e5cd', 0.28, 0.3, { emissive: '#08a998', emissiveIntensity: 2.0 }),
    headlamp: material('#fff4cf', 0.18, 0.16, { emissive: '#ffbf55', emissiveIntensity: 2.4 }),
    alarmGreen: material('#31dfa0', 0.2, 0.1, { emissive: '#0ab76e', emissiveIntensity: 2.4 }),
    alarmRed: material('#ff365f', 0.2, 0.1, { emissive: '#ec1042', emissiveIntensity: 2.8 }),
    tape: material('#f4ead3', 0.8, 0.02),
    cargoBlue: material('#174562', 0.52, 0.38),
    cargoTop: material('#213941', 0.46, 0.44),
  }
  parcelMaterials.tape = materials.tape

  const lights = []
  const lighting = {
    hemisphere: new THREE.HemisphereLight('#ccebe6', '#14232a', 0.86),
    key: new THREE.DirectionalLight('#fff0d2', 2.55),
    fill: new THREE.PointLight('#12c9bb', 7, 38, 2),
    rim: new THREE.DirectionalLight('#40b6dc', 0.72),
  }
  lighting.key.position.set(-10, 18, 12)
  lighting.key.castShadow = true
  lighting.key.shadow.mapSize.set(2048, 2048)
  lighting.key.shadow.camera.left = -20
  lighting.key.shadow.camera.right = 20
  lighting.key.shadow.camera.top = 20
  lighting.key.shadow.camera.bottom = -20
  lighting.fill.position.set(6, 8, -7)
  lighting.rim.position.set(10, 10, -12)
  root.add(lighting.hemisphere, lighting.key, lighting.fill, lighting.rim)
  const dynamic = {
    sourcePackages: [],
    truckPackages: [],
    carrierWheels: [],
    deckRollers: [],
    conveyorRollers: [],
    pickup: null,
    carrier: null,
    deck: null,
    carrierParcel: null,
    pickupParcel: null,
    transferParcel: null,
    pickerReach: null,
    pickerInnerReach: null,
    pickerDropRod: null,
    pickerTool: null,
    gripper: null,
    gripperLeft: null,
    gripperRight: null,
    loaderArm: null,
    loaderReach: null,
    loaderInnerReach: null,
    loaderTool: null,
    alarmLens: null,
    alarmPoint: null,
    agvStatus: null,
  }

  function addBox(parent, mat, size, position, options = {}) {
    return addMesh(parent, boxGeometry, mat, position, size, options)
  }

  function addRoundedBox(parent, mat, size, position, options = {}) {
    return addMesh(parent, roundedBoxGeometry, mat, position, size, options)
  }

  function addCylinder(parent, mat, radius, height, position, options = {}) {
    const mesh = addMesh(parent, cylinderGeometry, mat, position, [radius, height, radius], options)
    if (options.rotation) mesh.rotation.set(...options.rotation)
    return mesh
  }

  function addEmissiveLine(parent, size, position, color = '#37e5cd') {
    const emissive = material(color, 0.26, 0.18, { emissive: color, emissiveIntensity: 2.0 })
    return addBox(parent, emissive, size, position, { castShadow: false })
  }

  function addWheel(parent, geometry, x, y, z, radius, width, axleAxis = 'z', materialRef = materials.rubber) {
    const wheel = new THREE.Group()
    wheel.position.set(x, y, z)
    if (axleAxis === 'z') wheel.rotation.x = Math.PI / 2
    else wheel.rotation.z = Math.PI / 2
    const tire = addMesh(wheel, geometry, materialRef, [0, 0, 0], [radius / 0.34, width / 0.22, radius / 0.34])
    const hub = addMesh(wheel, torusGeometry, materials.tireHub, [0, 0, 0], [radius * 0.46, radius * 0.46, width * 0.75], { rotation: [Math.PI / 2, 0, 0] })
    const cap = addMesh(wheel, cylinderGeometry, materials.graphite, [0, 0, 0], [radius * 0.22, width / 0.22 * 0.58, radius * 0.22])
    parent.add(wheel)
    return { wheel, tire, hub, cap }
  }

  function makeParcelAt(position, size, color, name) {
    return makeParcel(root, roundedBoxGeometry, boxGeometry, parcelMaterials, labelMaterial, position, size, color, name)
  }

  function makeCanvasSign(parent, position, size, texture = signTexture) {
    const sign = addMesh(parent, new THREE.PlaneGeometry(1, 1), material('#ffffff', 0.45, 0.15, {
      map: texture,
      emissive: '#145357',
      emissiveMap: texture,
      emissiveIntensity: 0.22,
      side: THREE.DoubleSide,
    }), position, [size[0], size[1], 1], { castShadow: false })
    return sign
  }

  function addPallet(position, width = 0.82, depth = 0.82) {
    const pallet = new THREE.Group()
    pallet.position.set(position[0], position[1], position[2])
    for (let index = -1; index <= 1; index += 1) {
      addBox(pallet, materials.wood, [width, 0.045, 0.13], [0, 0, index * depth * 0.36])
    }
    addBox(pallet, materials.wood, [0.13, 0.10, depth * 0.92], [-width * 0.34, -0.055, 0])
    addBox(pallet, materials.wood, [0.13, 0.10, depth * 0.92], [0, -0.055, 0])
    addBox(pallet, materials.wood, [0.13, 0.10, depth * 0.92], [width * 0.34, -0.055, 0])
    root.add(pallet)
    return pallet
  }

  // Dark sealed floor, reflective traffic lane and safety paint give the cell a clear scale.
  addBox(root, materials.foundation, [22.2, 0.45, 21.0], [0, -0.31, 1.8])
  addBox(root, materials.concrete, [21.8, 0.20, 20.6], [0, -0.08, 1.8], { castShadow: false })
  addBox(root, materials.apron, [18.4, 0.22, 4.4], [0, -0.09, 9.1], { castShadow: false })
  addBox(root, materials.aisle, [11.4, 0.026, 2.0], [0.7, 0.027, -0.12], { castShadow: false })
  for (const z of [-1.16, 0.92]) {
    addBox(root, materials.marking, [11.0, 0.018, 0.052], [0.7, 0.046, z], { castShadow: false })
    addEmissiveLine(root, [10.6, 0.009, 0.018], [0.7, 0.058, z + (z < 0 ? 0.09 : -0.09)], '#1bb9b2')
  }
  for (let x = -8; x <= 8; x += 1) {
    addBox(root, materials.darkSteel, [0.018, 0.008, 15.8], [x, 0.025, 1.3], { castShadow: false })
  }
  for (const z of [7.08, 10.92]) {
    addBox(root, materials.marking, [15.2, 0.02, 0.045], [0, 0.045, z], { castShadow: false })
  }
  for (let x = -7.4; x <= 7.4; x += 0.5) {
    addBox(root, x % 1 === 0 ? materials.marking : materials.graphite, [0.24, 0.018, 0.18], [x, 0.045, 8.0], { castShadow: false })
  }

  // Enclose the working area with corrugated warehouse walls and clerestory glazing.
  const warehouseSideX = 10.55
  const warehouseBackZ = -8.42
  const dockWallZ = 5.52
  const sideWallLength = dockWallZ - warehouseBackZ
  const sideWallCenterZ = (dockWallZ + warehouseBackZ) / 2
  const sideWallBayLength = sideWallLength / 4
  for (const side of [-1, 1]) {
    const x = side * warehouseSideX
    addBox(root, materials.warehouseWall, [0.42, 1.12, sideWallLength], [x, 0.56, sideWallCenterZ])
    for (let bayIndex = 0; bayIndex < 4; bayIndex += 1) {
      const z = warehouseBackZ + sideWallBayLength * (bayIndex + 0.5)
      addBox(root, materials.wallCladding, [0.20, 3.78, sideWallBayLength], [x, 2.99, z])
      addBox(root, materials.wallGlass, [0.15, 1.28, sideWallBayLength], [x, 5.61, z], { castShadow: false })
      for (let ribZ = z - sideWallBayLength / 2 + 0.18; ribZ <= z + sideWallBayLength / 2 - 0.18; ribZ += 0.62) {
        addBox(root, materials.wallRib, [0.23, 3.82, 0.055], [x, 2.99, ribZ], { castShadow: false })
      }
    }
    addBox(root, materials.dockFrame, [0.27, 0.12, sideWallLength], [x, 1.19, sideWallCenterZ], { castShadow: false })
    addBox(root, materials.wallRib, [0.30, 0.20, sideWallLength], [x, 6.38, sideWallCenterZ], { castShadow: false })
  }

  // The rear wall closes the end of the hall while high windows preserve depth and light.
  addBox(root, materials.warehouseWall, [21.10, 1.12, 0.42], [0, 0.56, warehouseBackZ])
  for (const x of [-8.42, -4.21, 0, 4.21, 8.42]) {
    addBox(root, materials.wallCladding, [4.20, 3.78, 0.20], [x, 2.99, warehouseBackZ])
    addBox(root, materials.wallGlass, [4.20, 1.28, 0.15], [x, 5.61, warehouseBackZ], { castShadow: false })
    for (let ribX = x - 1.70; ribX <= x + 1.70; ribX += 0.62) {
      addBox(root, materials.wallRib, [0.055, 3.82, 0.23], [ribX, 2.99, warehouseBackZ], { castShadow: false })
    }
  }
  addBox(root, materials.wallRib, [21.10, 0.20, 0.30], [0, 6.38, warehouseBackZ], { castShadow: false })

  // The front wall has two open loading docks that align with the parked trucks.
  for (const [x, width] of [[-8.60, 3.80], [0, 6.20], [8.60, 3.80]]) {
    addBox(root, materials.wallCladding, [width, 6.58, 0.22], [x, 3.29, dockWallZ])
    for (let ribX = x - width / 2 + 0.22; ribX <= x + width / 2 - 0.22; ribX += 0.62) {
      addBox(root, materials.wallRib, [0.055, 6.50, 0.26], [ribX, 3.29, dockWallZ], { castShadow: false })
    }
  }
  for (const [index, dockX] of [-4.9, 4.9].entries()) {
    addBox(root, materials.wallCladding, [3.60, 2.12, 0.22], [dockX, 5.52, dockWallZ])
    addRoundedBox(root, materials.graphite, [3.96, 0.40, 0.40], [dockX, 4.42, 5.32])
    addBox(root, materials.dockFrame, [0.14, 4.42, 0.30], [dockX - 1.84, 2.21, 5.34])
    addBox(root, materials.dockFrame, [0.14, 4.42, 0.30], [dockX + 1.84, 2.21, 5.34])
    addBox(root, materials.graphite, [3.70, 0.22, 0.40], [dockX, 0.30, 5.34])
    addEmissiveLine(root, [2.80, 0.045, 0.025], [dockX, 4.03, 5.27], '#ffc35a')
    const dockSign = makeCanvasSign(root, [dockX, 6.08, 5.37], [2.55, 0.55], createTruckMarkTexture(renderer, textures, `DOCK 0${index + 1}`))
    dockSign.rotation.y = Math.PI
  }
  const centralDockSign = makeCanvasSign(root, [0, 5.37, 5.37], [3.85, 0.66])
  centralDockSign.rotation.y = Math.PI
  addBox(root, materials.graphite, [4.10, 0.84, 0.15], [0, 5.37, 5.46])

  // A high-bay, multi-tier storage wall; its length follows the full AGV travel lane.
  const rackFrontZ = -2.35
  const rackBackZ = -4.22
  const rackMinX = -4.9
  const rackMaxX = 6.15
  const rackCenterX = (rackMinX + rackMaxX) / 2
  const rackLength = rackMaxX - rackMinX
  const rackXs = Array.from({ length: 8 }, (_, index) => rackMinX + index * rackLength / 7)
  for (const x of rackXs) {
    for (const z of [rackFrontZ, rackBackZ]) {
      addBox(root, materials.rackUpright, [0.15, 5.52, 0.16], [x, 2.78, z])
      addBox(root, materials.graphite, [0.31, 0.07, 0.30], [x, 0.07, z])
      for (let y = 0.45; y < 5.25; y += 0.28) {
        addBox(root, materials.darkSteel, [0.17, 0.035, 0.175], [x, y, z + 0.002], { castShadow: false })
      }
    }
    addBar(root, cylinderGeometry, materials.rackOrange, [x, 0.42, rackBackZ], [x, 5.18, rackFrontZ], 0.035)
    addBar(root, cylinderGeometry, materials.rackOrange, [x, 5.18, rackBackZ], [x, 0.42, rackFrontZ], 0.035)
  }
  for (const y of [0.48, 1.94, 3.72, 5.20]) {
    for (const z of [rackFrontZ, rackBackZ]) {
      addBox(root, materials.rackOrange, [rackLength, 0.14, 0.11], [rackCenterX, y, z])
    }
    addBox(root, materials.rackDeck, [rackLength, 0.09, 1.68], [rackCenterX, y - 0.11, -3.30])
    for (let x = rackMinX + 0.06; x <= rackMaxX - 0.06; x += 0.38) {
      addBox(root, materials.darkSteel, [0.035, 0.04, 1.52], [x, y - 0.045, -3.30], { castShadow: false })
    }
  }
  for (let index = 0; index < SOURCE_PACKAGE_X.length; index += 1) {
    const size = PACKAGE_SIZES[index]
    const x = SOURCE_PACKAGE_X[index]
    addPallet([x, 1.965, -3.08], Math.max(size[0] + 0.20, 0.72), Math.max(size[2] + 0.16, 0.78))
    const parcel = makeParcelAt([x, 2.01 + size[1] / 2, -3.08], size, PACKAGE_PALETTE[index % 3], `ShelfParcel_${index + 1}`)
    dynamic.sourcePackages.push(parcel)
  }
  for (const [tierIndex, shelfY, z] of [[0, 0.48, -3.08], [1, 3.72, -3.54], [2, 5.20, -3.08]]) {
    let cartonIndex = 0
    for (let x = rackMinX + 0.35; x <= rackMaxX - 0.35; x += tierIndex === 1 ? 0.92 : 1.18) {
      if (tierIndex === 0 && x < -3.9) continue
      const size = tierIndex === 2 ? [0.72, 0.46, 0.62] : [0.62, 0.55, 0.54]
      const color = PACKAGE_PALETTE[(cartonIndex + tierIndex + 1) % 3]
      const shelfTop = shelfY - 0.065
      makeParcelAt([x, shelfTop + size[1] / 2, z], size, color, `ReserveStock_${tierIndex}_${cartonIndex}`)
      cartonIndex += 1
    }
  }
  for (const x of [-5.35, 0.72]) {
    addBox(root, materials.marking, [0.06, 0.014, 2.2], [x, 0.04, -3.29], { castShadow: false })
  }
  addBox(root, materials.darkSteel, [0.18, 0.08, 0.20], [-2.3, 0.09, -2.12])

  // Overhead picking rail with a travelling trolley, telescopic reach and a closing gripper.
  for (const x of [rackMinX + 0.35, rackMaxX - 0.35]) {
    addBox(root, materials.rackUpright, [0.21, 3.23, 0.24], [x, 1.63, -3.08])
    addBox(root, materials.graphite, [0.43, 0.075, 0.40], [x, 0.08, -3.08])
  }
  const pickerRailLength = rackLength - 0.70
  addBox(root, materials.rackOrange, [pickerRailLength, 0.17, 0.34], [rackCenterX, 3.43, -3.08])
  addBox(root, materials.brightMetal, [pickerRailLength - 0.26, 0.07, 0.20], [rackCenterX, 3.31, -3.08])
  addEmissiveLine(root, [pickerRailLength - 0.44, 0.024, 0.035], [rackCenterX, 3.21, -3.08], '#35d9c4')
  const picker = new THREE.Group()
  picker.position.set(-3.5, 3.3, -3.08)
  root.add(picker)
  addRoundedBox(picker, materials.bluePaint, [0.72, 0.23, 0.55], [0, 0.02, 0], { name: 'Picker carriage' })
  addBox(picker, materials.graphite, [0.50, 0.09, 0.35], [0, -0.13, 0])
  addBox(picker, materials.rackOrange, [0.24, 0.12, 0.27], [0, -0.20, 0.02])
  addEmissiveLine(picker, [0.48, 0.025, 0.025], [0, 0.02, 0.279])
  dynamic.pickerReach = addRoundedBox(picker, materials.brightMetal, [0.25, 0.16, 1], [0, -0.30, 0], { castShadow: true })
  dynamic.pickerReach.visible = false
  dynamic.pickerInnerReach = addBox(picker, materials.rackOrange, [0.14, 0.11, 1], [0, -0.30, 0.02])
  dynamic.pickerInnerReach.visible = false
  dynamic.pickerDropRod = addCylinder(picker, materials.metal, 0.075, 1, [0, -0.15, 0])
  dynamic.pickerTool = new THREE.Group()
  dynamic.pickerTool.position.set(0, -0.30, 0)
  picker.add(dynamic.pickerTool)
  dynamic.gripper = new THREE.Group()
  dynamic.pickerTool.add(dynamic.gripper)
  addRoundedBox(dynamic.gripper, materials.darkSteel, [0.60, 0.15, 0.52], [0, 0.08, 0])
  addBox(dynamic.gripper, materials.glow, [0.38, 0.025, 0.30], [0, 0.16, 0], { castShadow: false })
  dynamic.gripperLeft = new THREE.Group()
  dynamic.gripperRight = new THREE.Group()
  dynamic.gripper.add(dynamic.gripperLeft, dynamic.gripperRight)
  for (const jaw of [dynamic.gripperLeft, dynamic.gripperRight]) {
    addRoundedBox(jaw, materials.goldPaint, [0.095, 0.52, 0.13], [0, -0.15, 0])
    addBox(jaw, materials.darkSteel, [0.15, 0.10, 0.16], [0, -0.40, 0])
    addBox(jaw, materials.glow, [0.08, 0.035, 0.12], [0, -0.43, 0.01], { castShadow: false })
  }
  dynamic.pickupParcel = makeParcel(dynamic.gripper, roundedBoxGeometry, boxGeometry, parcelMaterials, labelMaterial, [0, -0.12, 0], PACKAGE_SIZES[0], 'blue', 'PickTransferParcel')
  dynamic.pickupParcel.group.visible = false
  dynamic.pickup = picker

  // A second double-sided pallet rack sits inside the hall between the truck bays.
  const middleRackMinX = -3.25
  const middleRackMaxX = 3.25
  const middleRackCenterX = (middleRackMinX + middleRackMaxX) / 2
  const middleRackLength = middleRackMaxX - middleRackMinX
  const middleRackFrontZ = 2.50
  const middleRackBackZ = 4.08
  const middleRackPosts = Array.from({ length: 5 }, (_, index) => middleRackMinX + index * middleRackLength / 4)
  for (const x of middleRackPosts) {
    for (const z of [middleRackFrontZ, middleRackBackZ]) {
      addBox(root, materials.rackUpright, [0.14, 4.62, 0.14], [x, 2.31, z])
      addBox(root, materials.graphite, [0.28, 0.07, 0.28], [x, 0.07, z])
    }
  }
  for (const levelY of [0.55, 1.80, 3.05, 4.30]) {
    for (const z of [middleRackFrontZ, middleRackBackZ]) {
      addBox(root, materials.rackOrange, [middleRackLength, 0.13, 0.11], [middleRackCenterX, levelY, z])
    }
    addBox(root, materials.rackDeck, [middleRackLength, 0.09, 1.78], [middleRackCenterX, levelY - 0.11, (middleRackFrontZ + middleRackBackZ) / 2])
    for (let x = middleRackMinX + 0.10; x <= middleRackMaxX - 0.10; x += 0.32) {
      addBox(root, materials.darkSteel, [0.035, 0.04, 1.64], [x, levelY - 0.045, (middleRackFrontZ + middleRackBackZ) / 2], { castShadow: false })
    }
  }
  for (let bayIndex = 0; bayIndex < middleRackPosts.length - 1; bayIndex += 1) {
    const xStart = middleRackPosts[bayIndex]
    const xEnd = middleRackPosts[bayIndex + 1]
    for (const z of [middleRackFrontZ, middleRackBackZ]) {
      addBar(root, cylinderGeometry, materials.rackOrange, [xStart, 0.42, z], [xEnd, 1.66, z], 0.026)
      addBar(root, cylinderGeometry, materials.rackOrange, [xStart, 1.66, z], [xEnd, 0.42, z], 0.026)
    }
  }
  const middleRackPackageXs = middleRackPosts.slice(0, -1).map((x, index) => (x + middleRackPosts[index + 1]) / 2)
  let middleRackPackageIndex = 0
  for (const levelY of [0.55, 1.80, 3.05]) {
    for (const z of [2.88, 3.70]) {
      for (const x of middleRackPackageXs) {
        const size = PACKAGE_SIZES[middleRackPackageIndex % PACKAGE_SIZES.length]
        const color = PACKAGE_PALETTE[middleRackPackageIndex % PACKAGE_PALETTE.length]
        const deckTop = levelY - 0.065
        addPallet([x, deckTop, z], Math.max(size[0] + 0.18, 0.76), Math.max(size[2] + 0.16, 0.72))
        makeParcelAt([x, deckTop + 0.05 + size[1] / 2, z], size, color, `MiddleRackStock_${middleRackPackageIndex + 1}`)
        middleRackPackageIndex += 1
      }
    }
  }

  // Main autonomous carrier: a low, four-wheel powered trolley with a liftable roller deck.
  const carrier = new THREE.Group()
  carrier.position.set(-3.5, 0, -0.12)
  root.add(carrier)
  addRoundedBox(carrier, materials.bluePaint, [1.48, 0.36, 1.22], [0, 0.38, 0], { name: 'Carrier chassis' })
  addRoundedBox(carrier, materials.blueDark, [1.22, 0.18, 0.92], [0, 0.58, -0.02])
  addBox(carrier, materials.rackOrange, [1.25, 0.055, 0.08], [0, 0.43, 0.60])
  addBox(carrier, materials.graphite, [0.88, 0.10, 0.12], [0, 0.64, 0.44])
  addBox(carrier, materials.whitePaint, [0.58, 0.13, 0.10], [0, 0.75, 0.42])
  for (const x of [-0.46, 0.46]) {
    addRoundedBox(carrier, materials.headlamp, [0.22, 0.13, 0.08], [x, 0.48, 0.63], { castShadow: false })
    addEmissiveLine(carrier, [0.09, 0.024, 0.11], [x, 0.30, 0.62], '#ffae47')
  }
  for (const x of [-0.52, 0.52]) {
    for (const z of [-0.49, 0.49]) {
      const wheel = addWheel(carrier, wheelGeometry, x, 0.25, z, 0.28, 0.24, 'z')
      dynamic.carrierWheels.push(wheel)
    }
  }
  for (const x of [-0.55, 0.55]) {
    addBox(carrier, materials.goldPaint, [0.045, 0.14, 0.84], [x, 0.69, -0.02])
  }
  addBox(carrier, materials.glow, [0.06, 0.055, 0.72], [0, 0.63, -0.02], { castShadow: false })
  addRoundedBox(carrier, materials.graphite, [0.66, 0.12, 0.19], [0, 0.82, -0.38])
  addBox(carrier, materials.rackOrange, [0.05, 0.14, 0.12], [0, 0.92, -0.38])
  dynamic.agvStatus = addMesh(carrier, sphereGeometry, materials.glow, [0, 0.95, -0.48], [0.10, 0.10, 0.10], { castShadow: false })
  dynamic.deck = new THREE.Group()
  dynamic.deck.position.set(0, 0.46, 0.85)
  carrier.add(dynamic.deck)
  addRoundedBox(dynamic.deck, materials.graphite, [1.44, 0.10, 1.38], [0, 0.015, 0])
  addBox(dynamic.deck, materials.darkSteel, [1.30, 0.025, 1.22], [0, 0.08, 0])
  for (let index = 0; index < 7; index += 1) {
    const x = -0.57 + index * 0.19
    const roller = new THREE.Group()
    roller.position.set(x, 0.135, 0)
    roller.rotation.z = Math.PI / 2
    const mesh = addMesh(roller, rollerGeometry, materials.metal, [0, 0, 0], [1, 1, 1])
    mesh.rotation.y = 0
    dynamic.deckRollers.push(mesh)
    dynamic.deck.add(roller)
  }
  for (const z of [-0.62, 0.62]) {
    addRoundedBox(dynamic.deck, materials.rackOrange, [1.34, 0.16, 0.06], [0, 0.18, z])
    addEmissiveLine(dynamic.deck, [1.20, 0.018, 0.02], [0, 0.275, z], '#ffc35a')
  }
  for (const x of [-0.66, 0.66]) {
    addBox(dynamic.deck, materials.rackOrange, [0.06, 0.16, 1.20], [x, 0.18, 0])
  }
  dynamic.carrierParcel = makeParcel(dynamic.deck, roundedBoxGeometry, boxGeometry, parcelMaterials, labelMaterial, [0, 0.13 + PACKAGE_SIZES[0][1] / 2, 0], PACKAGE_SIZES[0], 'blue', 'CarrierParcel')
  dynamic.carrierParcel.group.visible = false
  dynamic.carrier = carrier

  // A detailed powered roller conveyor meets the open rear of the outbound truck.
  addBox(root, materials.graphite, [1.12, 0.30, 4.98], [4.9, 0.48, 3.09])
  addBox(root, materials.belt, [0.98, 0.055, 4.86], [4.9, 0.66, 3.09])
  for (let index = 0; index < 18; index += 1) {
    const z = 0.78 + index * 0.27
    const roller = new THREE.Group()
    roller.position.set(4.9, 0.64, z)
    roller.rotation.z = Math.PI / 2
    const mesh = addMesh(roller, rollerGeometry, materials.metal, [0, 0, 0], [1, 1, 1])
    mesh.rotation.y = 0
    dynamic.conveyorRollers.push(mesh)
    root.add(roller)
  }
  for (const x of [4.38, 5.42]) {
    addRoundedBox(root, materials.bluePaint, [0.08, 0.28, 4.22], [x, 0.84, 3.40])
    addEmissiveLine(root, [0.025, 0.05, 3.86], [x, 1.00, 3.40])
  }
  for (const z of [0.67, 5.49]) {
    addBox(root, materials.rackOrange, [1.20, 0.14, 0.10], [4.9, 0.81, z])
  }
  for (const x of [4.54, 5.26]) {
    for (const z of [1.15, 2.55, 3.95, 5.05]) {
      addBox(root, materials.darkSteel, [0.10, 0.40, 0.11], [x, 0.28, z])
      addBox(root, materials.metal, [0.38, 0.06, 0.38], [x, 0.07, z])
    }
  }

  // Telescopic loader follows the live package coordinates into the trailer cargo slots.
  addRoundedBox(root, materials.graphite, [0.86, 0.24, 0.82], [4.9, 0.68, 5.12])
  addCylinder(root, materials.bluePaint, 0.16, 0.94, [4.9, 1.25, 5.12])
  const loader = new THREE.Group()
  loader.position.set(4.9, 1.54, 5.12)
  root.add(loader)
  dynamic.loaderArm = loader
  addRoundedBox(loader, materials.rackOrange, [0.58, 0.46, 0.58], [0, 0, 0])
  addBox(loader, materials.graphite, [0.38, 0.12, 0.42], [0, -0.25, 0])
  dynamic.loaderReach = addRoundedBox(loader, materials.brightMetal, [0.23, 0.20, 1], [0, 0, 0])
  dynamic.loaderReach.visible = false
  dynamic.loaderInnerReach = addBox(loader, materials.rackOrange, [0.14, 0.11, 1], [0, 0.03, 0.02])
  dynamic.loaderInnerReach.visible = false
  dynamic.loaderTool = new THREE.Group()
  loader.add(dynamic.loaderTool)
  addRoundedBox(dynamic.loaderTool, materials.bluePaint, [0.52, 0.30, 0.28], [0, 0, 0])
  for (const x of [-0.20, 0.20]) {
    addBox(dynamic.loaderTool, materials.goldPaint, [0.10, 0.28, 0.34], [x, -0.20, 0.12])
  }
  dynamic.transferParcel = makeParcelAt([4.9, 0.94, 0.73], PACKAGE_SIZES[0], 'blue', 'TransferParcel')
  dynamic.transferParcel.group.visible = false

  // Two docked trucks provide scale; the open outbound trailer exposes all six live cargo slots.
  function addTruck(x, z, openRear, bodyMaterial) {
    const truck = new THREE.Group()
    truck.position.set(x, 0, z)
    root.add(truck)
    addRoundedBox(truck, materials.graphite, [2.34, 0.32, 4.72], [0, 0.68, 0])
    if (openRear) {
      addRoundedBox(truck, materials.whitePaint, [2.62, 0.12, 3.10], [0, 0.79, -0.72])
      addBox(truck, bodyMaterial, [2.72, 2.52, 0.13], [0, 2.0, 0.82])
      for (const side of [-1, 1]) {
        addBox(truck, bodyMaterial, [0.11, 2.44, 3.10], [side * 1.31, 2.0, -0.72])
        addBox(truck, materials.rackOrange, [0.07, 2.40, 0.06], [side * 1.37, 2.0, -0.72])
      }
      addBox(truck, materials.graphite, [2.78, 0.12, 3.22], [0, 3.31, -0.72])
      for (const side of [-1, 1]) {
        const door = addBox(truck, materials.whitePaint, [1.18, 2.38, 0.10], [side * 1.80, 2.0, -2.26])
        door.rotation.y = side * 0.26
        addBox(truck, materials.bluePaint, [0.06, 2.25, 0.12], [side * 1.80, 2.0, -2.26])
      }
      for (let index = 0; index < CARGO_SLOTS.length; index += 1) {
        const [slotX, slotZ] = CARGO_SLOTS[index]
        const size = PACKAGE_SIZES[index]
        const color = PACKAGE_PALETTE[index % PACKAGE_PALETTE.length]
        const parcel = makeParcelAt([x + slotX, 0.85 + size[1] / 2, z + (slotZ - 7.85)], size, color, `TruckParcel_${index + 1}`)
        dynamic.truckPackages.push(parcel)
      }
      makeCanvasSign(truck, [-1.38, 2.40, -0.72], [1.14, 0.42], createTruckMarkTexture(renderer, textures, 'DL / OUTBOUND'))
    } else {
      addRoundedBox(truck, bodyMaterial, [2.72, 2.54, 3.18], [0, 2.0, -0.72])
      addRoundedBox(truck, materials.whitePaint, [2.78, 0.13, 3.24], [0, 3.31, -0.72])
      for (const side of [-1, 1]) {
        addBox(truck, materials.bluePaint, [0.055, 0.12, 3.12], [side * 1.39, 1.50, -0.72])
      }
    }
    addRoundedBox(truck, materials.bluePaint, [2.18, 1.50, 1.70], [0, 1.25, 1.92])
    addRoundedBox(truck, materials.blueDark, [2.10, 0.20, 1.64], [0, 2.08, 1.93])
    addBox(truck, materials.glass, [1.56, 0.60, 0.07], [0, 1.72, 2.79])
    addBox(truck, materials.whitePaint, [1.86, 0.13, 0.10], [0, 0.70, 2.84])
    for (const side of [-1, 1]) {
      addRoundedBox(truck, materials.headlamp, [0.30, 0.22, 0.08], [side * 0.78, 1.0, 2.84], { castShadow: false })
      addBox(truck, materials.graphite, [0.12, 0.14, 0.36], [side * 1.19, 1.57, 2.45])
      for (const wheelZ of [-2.0, -1.35, 0.78, 2.03]) {
        addWheel(truck, wheelGeometry, side * 1.35, 0.42, wheelZ, 0.39, 0.30, 'x')
      }
      for (let ribZ = -1.90; ribZ <= 0.80; ribZ += 0.45) {
        if (!openRear) addBox(truck, materials.metal, [0.035, 2.34, 0.04], [side * 1.40, 2.0, ribZ])
      }
    }
    addBox(truck, materials.rackOrange, [2.32, 0.24, 0.20], [0, 0.68, 2.82])
    for (const side of [-1, 1]) {
      addRoundedBox(truck, materials.glow, [0.16, 0.06, 0.08], [side * 0.83, 0.92, 2.87], { castShadow: false })
    }
    if (openRear) {
      for (const xOffset of [-1.48, 1.48]) {
        addBox(truck, materials.rackOrange, [0.13, 2.52, 0.14], [xOffset, 2.02, -2.31])
      }
      addBox(truck, materials.rackOrange, [3.00, 0.14, 0.16], [0, 3.30, -2.31])
    } else {
      addBox(truck, materials.whitePaint, [2.76, 2.58, 0.13], [0, 2.0, -2.34])
    }
    return truck
  }

  function createTruckMarkTexture(rendererRef, textureList, title) {
    const canvas = document.createElement('canvas')
    canvas.width = 512
    canvas.height = 160
    const context = canvas.getContext('2d')
    context.fillStyle = '#edf3ef'
    context.fillRect(0, 0, 512, 160)
    context.fillStyle = '#0e6981'
    context.fillRect(0, 0, 512, 22)
    context.fillStyle = '#163b48'
    context.font = '800 46px Arial, sans-serif'
    context.fillText(title, 24, 93)
    context.font = '600 21px Arial, sans-serif'
    context.fillStyle = '#d27b32'
    context.fillText('SMART LOGISTICS  -  ZERO EMISSION', 25, 132)
    const texture = makeCanvasTexture(rendererRef, canvas)
    textureList.push(texture)
    return texture
  }

  // The truck marking helper is declared before its first invocation at runtime.
  addTruck(-4.9, 7.85, false, materials.whitePaint)
  addTruck(4.9, 7.85, true, materials.whitePaint)
  makeCanvasSign(root, [0, 5.78, -5.98], [4.45, 1.12])
  addBox(root, materials.graphite, [4.72, 1.34, 0.15], [0, 5.78, -6.04])
  for (const x of [-8.25, 8.25]) {
    for (const z of [-6.0, -2.1, 2.0, 5.5]) {
      addBox(root, materials.darkSteel, [0.32, 6.3, 0.32], [x, 3.18, z])
      addBox(root, materials.rackOrange, [0.42, 0.08, 0.42], [x, 0.12, z])
    }
    addBox(root, materials.rackUpright, [0.44, 0.30, 12.0], [x, 6.30, -0.20])
  }
  for (const z of [-6.0, -2.1, 2.0, 5.5]) {
    addBox(root, materials.darkSteel, [16.8, 0.28, 0.32], [0, 6.30, z])
  }
  for (const x of [-6.7, -2.3, 2.3, 6.7]) {
    addBar(root, cylinderGeometry, materials.rackOrange, [x, 6.28, -6.0], [x * 0.88, 5.65, 5.8], 0.035)
    addBar(root, cylinderGeometry, materials.rackOrange, [x, 6.28, 5.5], [x * 0.88, 5.65, -6.0], 0.035)
  }
  for (const [index, z] of [-4.8, -1.2, 2.4, 5.2].entries()) {
    const lightHousing = addRoundedBox(root, materials.graphite, [4.4, 0.16, 0.40], [0, 6.0, z])
    lightHousing.castShadow = false
    addEmissiveLine(root, [3.9, 0.035, 0.10], [0, 5.88, z], index % 2 ? '#c5efff' : '#ffe1b1')
    const point = new THREE.PointLight(index % 2 ? '#b8efff' : '#ffd294', 12, 18, 1.7)
    point.position.set(index % 2 ? 2.2 : -2.2, 5.65, z)
    root.add(point)
    lights.push(point)
  }
  for (const x of [-7.3, -5.4, 5.4, 7.3]) {
    addBox(root, materials.marking, [0.045, 0.018, 3.8], [x, 0.045, 8.95], { castShadow: false })
  }
  for (const x of [-7.4, -2.3, 2.3, 7.4]) {
    addCylinder(root, materials.rackOrange, 0.12, 0.96, [x, 0.51, 5.72])
    addRoundedBox(root, materials.marking, [0.16, 0.08, 0.20], [x, 0.12, 5.72])
  }
  for (const bayX of [-4.9, 4.9]) {
    addBox(root, materials.rackOrange, [3.75, 0.42, 0.35], [bayX, 5.48, 5.46])
    for (const side of [-1, 1]) {
      addBox(root, materials.bluePaint, [0.25, 4.0, 0.34], [bayX + side * 1.72, 3.38, 5.46])
      addEmissiveLine(root, [0.045, 3.55, 0.05], [bayX + side * 1.47, 3.30, 5.20], '#ffb241')
    }
    addBox(root, materials.graphite, [4.2, 0.16, 1.15], [bayX, 5.0, 6.10])
    addEmissiveLine(root, [3.8, 0.025, 0.08], [bayX, 4.89, 6.1], '#36deca')
  }

  const alarmBase = addRoundedBox(root, materials.graphite, [0.58, 0.30, 0.58], [4.95, 0.16, -1.78])
  addCylinder(root, materials.metal, 0.08, 0.42, [4.95, 0.48, -1.78])
  dynamic.alarmLens = addMesh(root, sphereGeometry, materials.alarmGreen, [4.95, 0.76, -1.78], [0.25, 0.32, 0.25], { castShadow: false })
  dynamic.alarmPoint = new THREE.PointLight('#31dfa0', 9, 5, 2)
  dynamic.alarmPoint.position.set(4.95, 0.85, -1.78)
  root.add(dynamic.alarmPoint)
  addBox(root, materials.graphite, [2.25, 1.07, 0.13], [0, 3.42, -4.55])
  makeCanvasSign(root, [0, 3.42, -4.475], [2.08, 0.98], createTruckMarkTexture(renderer, textures, 'CELL 04  /  LIVE'))
  for (let index = 0; index < 6; index += 1) {
    addRoundedBox(root, index % 2 ? materials.glow : materials.headlamp, [0.16, 0.18, 0.07], [-0.80 + index * 0.32, 2.72, -4.42], { castShadow: false })
  }
  void alarmBase

  const bounds = new THREE.Box3().setFromObject(root)
  const center = bounds.getCenter(new THREE.Vector3())
  const radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 1)

  function update(outputs = {}) {
    const loadCount = Math.max(0, Math.min(6, Number(outputs.truckLoadCount) || 0))
    const selectedIndex = Math.max(0, Math.min(5, Math.floor(loadCount)))
    for (let index = 0; index < dynamic.sourcePackages.length; index += 1) {
      const signal = outputs[`shelfPackage${index + 1}Visible`]
      if (typeof signal === 'boolean') dynamic.sourcePackages[index].group.visible = signal
      else if (index === selectedIndex && typeof outputs.sourcePackageVisible === 'boolean') {
        dynamic.sourcePackages[index].group.visible = outputs.sourcePackageVisible
      } else dynamic.sourcePackages[index].group.visible = index >= loadCount
    }
    for (let index = 0; index < dynamic.truckPackages.length; index += 1) {
      dynamic.truckPackages[index].group.visible = loadCount >= index + 1
    }

    const color = outputs.packageColorGold === true
      ? 'gold'
      : outputs.packageColorCoral === true
        ? 'coral'
        : 'blue'
    const size = [
      clamp(Number(outputs.packageSizeX), 0.36, 0.72, PACKAGE_SIZES[selectedIndex][0]),
      clamp(Number(outputs.packageSizeY), 0.30, 0.72, PACKAGE_SIZES[selectedIndex][1]),
      clamp(Number(outputs.packageSizeZ), 0.34, 0.76, PACKAGE_SIZES[selectedIndex][2]),
    ]
    for (const parcel of [dynamic.pickupParcel, dynamic.carrierParcel, dynamic.transferParcel]) {
      if (parcel) setParcelDimensions(parcel, size, color)
    }

    dynamic.pickupParcel.group.visible = outputs.pickupPackageVisible === true
    dynamic.carrierParcel.group.visible = outputs.packageOnAgv === true
    dynamic.transferParcel.group.visible = outputs.transferPackageVisible === true
    dynamic.transferParcel.group.position.set(
      clamp(Number(outputs.transferPackageX), -20, 20, 4.9),
      clamp(Number(outputs.transferPackageY), 0.25, 3.0, 0.94),
      clamp(Number(outputs.transferPackageZ), -1, 12, 0.73),
    )

    dynamic.carrier.position.x = clamp(Number(outputs.agvPosition), 0, 8.4, 0) - 3.5
    dynamic.carrier.position.z = -0.12
    dynamic.deck.position.y = clamp(Number(outputs.forkHeight), 0.30, 0.65, 0.46)
    const wheelPhase = -dynamic.carrier.position.x / 0.28
    for (const wheel of dynamic.carrierWheels) wheel.tire.rotation.y = wheelPhase
    const beltPhase = clamp(Number(outputs.beltPhase), -100, 100, 0)
    for (const roller of dynamic.deckRollers) roller.rotation.y = beltPhase
    for (const roller of dynamic.conveyorRollers) roller.rotation.y = beltPhase

    const armX = clamp(Number(outputs.pickupArmX), -5, 1, -3.5)
    const armY = clamp(Number(outputs.pickupArmY), 2.8, 3.8, 3.3)
    const armZ = clamp(Number(outputs.pickupArmZ), -4, -2, -3.08)
    const reach = clamp(Number(outputs.pickupArmReachProgress), 0, 3.81, 0)
    const drop = clamp(Number(outputs.pickupDropProgress), -2.7, -0.2, -0.3)
    const dropLength = Math.max(0.18, -drop)
    dynamic.pickup.position.set(armX, armY, armZ)
    dynamic.pickerReach.scale.z = Math.max(reach, 0.001)
    dynamic.pickerReach.position.z = reach * 0.5
    dynamic.pickerReach.visible = reach > 0.08
    dynamic.pickerInnerReach.scale.z = Math.max(reach * 0.88, 0.001)
    dynamic.pickerInnerReach.position.z = 0.10 + reach * 0.44
    dynamic.pickerInnerReach.visible = reach > 0.08
    dynamic.pickerDropRod.scale.y = dropLength
    dynamic.pickerDropRod.position.y = drop / 2
    dynamic.pickerTool.position.set(0, drop, reach)
    dynamic.gripper.rotation.x = clamp(Number(outputs.robotAngle), -0.4, 0.5, 0.12)
    const jawGap = clamp(Number(outputs.pickupGripperGap), 0.35, 1.2, 1.0) / 2
    dynamic.gripperLeft.position.x = -jawGap
    dynamic.gripperRight.position.x = jawGap
    dynamic.loaderArm.rotation.y = clamp(Number(outputs.loaderAngle), -0.7, 0.7, 0)
    const loaderReach = clamp(Number(outputs.loaderReach), 0, 2.4, 0)
    dynamic.loaderReach.scale.z = Math.max(loaderReach, 0.001)
    dynamic.loaderReach.position.z = loaderReach / 2
    dynamic.loaderReach.visible = loaderReach > 0.04
    dynamic.loaderInnerReach.scale.z = Math.max(loaderReach * 0.85, 0.001)
    dynamic.loaderInnerReach.position.z = loaderReach * 0.43 + 0.08
    dynamic.loaderInnerReach.visible = loaderReach > 0.04
    dynamic.loaderTool.position.z = loaderReach

    const alarm = outputs.alarm === true
    dynamic.alarmLens.material = alarm ? materials.alarmRed : materials.alarmGreen
    dynamic.alarmPoint.color.set(alarm ? '#ff365f' : '#31dfa0')
    dynamic.alarmPoint.intensity = alarm ? 16 : 5
    dynamic.agvStatus.material = outputs.orderComplete === true ? materials.alarmGreen : materials.glow
  }

  function clamp(value, min, max, fallback) {
    return Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback
  }

  function animate(timeSeconds) {
    if (dynamic.alarmLens) dynamic.alarmLens.scale.y = 0.32 + (dynamic.alarmPoint.intensity > 5 ? 0.08 * (0.5 + 0.5 * Math.sin(timeSeconds * 8)) : 0)
    if (dynamic.alarmPoint.intensity > 5) dynamic.alarmPoint.intensity = 11 + 8 * (0.5 + 0.5 * Math.sin(timeSeconds * 8))
    if (dynamic.agvStatus) dynamic.agvStatus.scale.setScalar(0.09 + 0.015 * (0.5 + 0.5 * Math.sin(timeSeconds * 2.5)))
  }

  function setDayMode(day) {
    lighting.hemisphere.intensity = day ? 1.25 : 0.86
    lighting.key.intensity = day ? 3.1 : 2.55
    lighting.fill.intensity = day ? 4.5 : 7
    lighting.rim.intensity = day ? 0.50 : 0.72
    for (const light of lights) light.intensity = day ? 7.5 : 12
  }

  function dispose() {
    root.traverse((node) => {
      if (node.geometry) node.geometry.dispose()
      if (Array.isArray(node.material)) node.material.forEach((entry) => entry.dispose())
      else if (node.material) node.material.dispose()
    })
    for (const texture of textures) texture.dispose()
    scene.remove(root)
  }

  return { root, center, radius, update, animate, setDayMode, dispose }
}

