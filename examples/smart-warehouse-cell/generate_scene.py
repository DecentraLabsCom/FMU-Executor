"""Build the self-contained GLB for the Smart Warehouse Cell example."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "smart-warehouse-cell.glb"
binary = bytearray()
buffer_views: list[dict] = []
accessors: list[dict] = []
meshes: list[dict] = []


def add_buffer(payload: bytes, target: int) -> int:
    while len(binary) % 4:
        binary.append(0)
    offset = len(binary)
    binary.extend(payload)
    buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(payload), "target": target})
    return len(buffer_views) - 1


def add_accessor(view: int, component_type: int, count: int, kind: str, *, min_value=None, max_value=None) -> int:
    accessor = {"bufferView": view, "componentType": component_type, "count": count, "type": kind}
    if min_value is not None:
        accessor["min"] = min_value
    if max_value is not None:
        accessor["max"] = max_value
    accessors.append(accessor)
    return len(accessors) - 1


def box_faces():
    return [
        ([(-.5,-.5,.5),(.5,-.5,.5),(.5,.5,.5),(-.5,.5,.5)], (0,0,1)),
        ([(.5,-.5,-.5),(-.5,-.5,-.5),(-.5,.5,-.5),(.5,.5,-.5)], (0,0,-1)),
        ([(-.5,-.5,-.5),(-.5,-.5,.5),(-.5,.5,.5),(-.5,.5,-.5)], (-1,0,0)),
        ([(.5,-.5,.5),(.5,-.5,-.5),(.5,.5,-.5),(.5,.5,.5)], (1,0,0)),
        ([(-.5,.5,.5),(.5,.5,.5),(.5,.5,-.5),(-.5,.5,-.5)], (0,1,0)),
        ([(-.5,-.5,-.5),(.5,-.5,-.5),(.5,-.5,.5),(-.5,-.5,.5)], (0,-1,0)),
    ]


def cylinder_faces(segments: int = 20):
    faces = []
    for index in range(segments):
        a = 2 * math.pi * index / segments
        b = 2 * math.pi * (index + 1) / segments
        p0 = (.5 * math.cos(a), -.5, .5 * math.sin(a))
        p1 = (.5 * math.cos(b), -.5, .5 * math.sin(b))
        p2 = (.5 * math.cos(b), .5, .5 * math.sin(b))
        p3 = (.5 * math.cos(a), .5, .5 * math.sin(a))
        normal = (math.cos((a+b)/2), 0, math.sin((a+b)/2))
        faces.append(([p0,p1,p2,p3], normal))
        faces.append(([(0,-.5,0),p1,p0,(0,-.5,0)], (0,-1,0)))
        faces.append(([(0,.5,0),p3,p2,(0,.5,0)], (0,1,0)))
    return faces


def add_mesh(name: str, material: int, faces=None) -> int:
    positions = []
    normals = []
    indices = []
    for corners, normal in faces or box_faces():
        start = len(positions)
        positions.extend(corners)
        normals.extend([normal] * len(corners))
        indices.extend((start, start + 1, start + 2, start, start + 2, start + 3))
    p_view = add_buffer(b"".join(struct.pack("<3f", *v) for v in positions), 34962)
    n_view = add_buffer(b"".join(struct.pack("<3f", *v) for v in normals), 34962)
    i_view = add_buffer(b"".join(struct.pack("<H", value) for value in indices), 34963)
    p_accessor = add_accessor(p_view, 5126, len(positions), "VEC3",
                              min_value=[min(v[i] for v in positions) for i in range(3)],
                              max_value=[max(v[i] for v in positions) for i in range(3)])
    n_accessor = add_accessor(n_view, 5126, len(normals), "VEC3")
    i_accessor = add_accessor(i_view, 5123, len(indices), "SCALAR")
    meshes.append({"name": name, "primitives": [{
        "attributes": {"POSITION": p_accessor, "NORMAL": n_accessor},
        "indices": i_accessor, "material": material, "mode": 4,
    }]})
    return len(meshes) - 1


materials = [
    {"name":"Obsidian frame","pbrMetallicRoughness":{"baseColorFactor":[.055,.095,.13,1],"metallicFactor":.72,"roughnessFactor":.3}},
    {"name":"Safety amber","pbrMetallicRoughness":{"baseColorFactor":[1,.54,.08,1],"metallicFactor":.22,"roughnessFactor":.34}},
    {"name":"Brushed alloy","pbrMetallicRoughness":{"baseColorFactor":[.48,.60,.67,1],"metallicFactor":.8,"roughnessFactor":.24}},
    {"name":"Aisle teal","pbrMetallicRoughness":{"baseColorFactor":[.03,.49,.51,1],"metallicFactor":.24,"roughnessFactor":.34}},
    {"name":"Parcel coral","pbrMetallicRoughness":{"baseColorFactor":[.96,.31,.16,1],"metallicFactor":.05,"roughnessFactor":.55}},
    {"name":"Parcel blue","pbrMetallicRoughness":{"baseColorFactor":[.12,.38,.82,1],"metallicFactor":.12,"roughnessFactor":.42}},
    {"name":"Parcel gold","pbrMetallicRoughness":{"baseColorFactor":[1,.72,.13,1],"metallicFactor":.08,"roughnessFactor":.46}},
    {"name":"Pallet mint","pbrMetallicRoughness":{"baseColorFactor":[.18,.78,.60,1],"metallicFactor":.18,"roughnessFactor":.36}},
    {"name":"Alarm lamp","emissiveFactor":[.55,.03,.01],"pbrMetallicRoughness":{"baseColorFactor":[.75,.04,.025,1],"metallicFactor":.06,"roughnessFactor":.25}},
    {"name":"Floor","pbrMetallicRoughness":{"baseColorFactor":[.045,.075,.09,1],"metallicFactor":.34,"roughnessFactor":.76}},
    {"name":"Glass cyan","emissiveFactor":[.02,.4,.45],"pbrMetallicRoughness":{"baseColorFactor":[.04,.42,.48,1],"metallicFactor":.38,"roughnessFactor":.25}},
]
box_meshes = [add_mesh(f"Box_{index}", index) for index in range(len(materials))]
wheel_mesh = add_mesh("Wheel", 0, cylinder_faces())
node_list: list[dict] = []


def add_node(name: str, mesh: int, position, scale, *, rotation=None, children=None, extras=None):
    node = {"name": name, "mesh": mesh, "translation": list(position), "scale": list(scale)}
    if rotation:
        node["rotation"] = list(rotation)
    if children:
        node["children"] = children
    if extras:
        node["extras"] = extras
    node_list.append(node)
    return len(node_list) - 1


def add_group(name: str, position, *, children=None, extras=None):
    node = {"name": name, "translation": list(position)}
    if children:
        node["children"] = children
    if extras:
        node["extras"] = extras
    node_list.append(node)
    return len(node_list) - 1


roots = []
roots.append(add_node("WarehouseFloor", box_meshes[9], (0,-.13,0), (13,.26,10)))
roots.append(add_node("CentralAisle", box_meshes[3], (0,.006,0), (10.8,.015,1.48)))
for z in (-.83,.83):
    roots.append(add_node(f"AisleSafetyLine_{z}", box_meshes[1], (0,.018,z), (10.6,.025,.055)))
for x in range(-5, 6):
    roots.append(add_node(f"FloorGuide_{x}", box_meshes[2], (x,.003,0), (.018,.008,8.6)))

# Two high-bay pallet racks frame a broad, open AGV aisle.
for row, z in enumerate((-3.55, 3.55)):
    for x in (-4.65,-2.35,-.05,2.25,4.55):
        roots.append(add_node(f"Rack_{row}_{x}_upright_a", box_meshes[0], (x,2.0,z), (.13,4.0,.16)))
        roots.append(add_node(f"Rack_{row}_{x}_upright_b", box_meshes[0], (x,2.0,z + (0.92 if row else -.92)), (.13,4.0,.16)))
    for shelf_y in (.45,1.72,2.98,4.0):
        roots.append(add_node(f"Rack_{row}_beam_{shelf_y}", box_meshes[1], (0,shelf_y,z), (9.35,.12,.2)))
    for x in (-3.5,-1.2,1.1,3.4):
        for level, y in enumerate((.98,2.24,3.52)):
            depth_sign = 1 if row else -1
            pallet = add_node(f"Pallet_{row}_{x}_{level}", box_meshes[7], (x,y-.29,z + depth_sign*.47), (1.58,.12,.74))
            roots.append(pallet)
            palette = (4,5,6)[(int(x*10) + level + row) % 3]
            carton = add_node(f"StockCarton_{row}_{x}_{level}", box_meshes[palette], (x,y,z + depth_sign*.47), (1.15,.54,.58))
            roots.append(carton)
            roots.append(add_node(f"StockLabel_{row}_{x}_{level}", box_meshes[2], (x,y+.03,z + depth_sign*.17), (.38,.16,.035)))

# The warehouse collection belt, rollers and sort gate at the rear.
roots.append(add_node("InboundConveyorFrame", box_meshes[0], (0,.55,-4.62), (7.4,.44,.92)))
roots.append(add_node("InboundConveyorBelt", box_meshes[3], (0,.80,-4.62), (7.15,.08,.78)))
for x in (-3.2,-2.4,-1.6,-.8,0,.8,1.6,2.4,3.2):
    roots.append(add_node(f"ConveyorRoller_{x}", wheel_mesh, (x,.55,-4.62), (.20,.98,.20), rotation=(0,0,.70710678,.70710678)))
roots.append(add_node("SortGate", box_meshes[1], (2.6,1.05,-4.62), (.18,.48,.95)))
roots.append(add_node("InboundParcel", box_meshes[4], (-2.6,1.06,-4.62), (.72,.50,.62)))

# Animated AGV parent moves through the aisle; the fork and parcel inherit its translation.
agv_children = [
    add_node("AgvChassis", box_meshes[0], (0,.30,0), (1.55,.42,1.08)),
    add_node("AgvNose", box_meshes[3], (.62,.44,0), (.46,.28,.94)),
    add_node("AgvDisplay", box_meshes[10], (0,.59,-.55), (.62,.25,.035)),
]
for x in (-.53,.53):
    agv_children.append(add_node(f"AgvWheel_{x}_front", wheel_mesh, (x,.16,.55), (.31,.23,.31)))
    agv_children.append(add_node(f"AgvWheel_{x}_rear", wheel_mesh, (x,.16,-.55), (.31,.23,.31)))
fork = add_node("AgvForks", box_meshes[2], (0,.46,.67), (1.04,.12,.46), extras={"fmiBindings":[
    {"variable":"forkHeight","target":"position.y","scale":1,"offset":0},
]})
agv_children.append(fork)
agv_children.append(add_node("WarehousePackage", box_meshes[6], (0,.86,.73), (.76,.62,.67), extras={"fmiBindings":[
    {"variable":"packageOnAgv","target":"visible"},
]}))
agv_group = add_group("WarehouseAGV", (-3.5,0,0), children=agv_children, extras={"fmiBindings":[
    {"variable":"agvPosition","target":"position.x","scale":1,"offset":-3.5},
]})
roots.append(agv_group)

# Servo pick arm and a safety light provide clear live status cues.
roots.append(add_node("PickCellBase", box_meshes[0], (-3.25,.72,-1.65), (1.5,.45,1.1)))
roots.append(add_node("PickCellColumn", box_meshes[2], (-3.25,1.72,-1.65), (.35,2.1,.35)))
arm_segment = add_node("PickArmLink", box_meshes[1], (0,.65,0), (.28,1.42,.30))
arm_tool = add_node("PickArmTool", box_meshes[3], (0,1.38,.1), (.62,.20,.48))
arm_group = add_group("PickArmPivot", (-3.25,2.46,-1.65), children=[arm_segment,arm_tool], extras={"fmiBindings":[
    {"variable":"robotAngle","target":"rotation.z","scale":-1,"offset":0},
]})
roots.append(arm_group)
roots.append(add_node("WarehouseAlarmBase", box_meshes[0], (4.95,.52,-1.78), (.58,.30,.58)))
roots.append(add_node("WarehouseAlarmLamp", box_meshes[8], (4.95,1.04,-1.78), (.34,.72,.34), extras={"fmiBindings":[
    {"variable":"alarm","target":"material.color","falseColor":"#24a875","trueColor":"#ff2d55"},
]}))
roots.append(add_node("WarehouseInfoPanel", box_meshes[10], (0,3.45,-4.30), (2.2,1.05,.09)))
for x in (-.78,-.45,-.12,.21,.54,.87):
    roots.append(add_node(f"PanelSignal_{x}", box_meshes[6], (x,3.45,-4.23), (.18,.18,.035)))

scene = {"name":"Smart Warehouse Twin", "nodes":roots}
gltf = {
    "asset":{"version":"2.0","generator":"DecentraLabs Smart Warehouse Cell"},
    "scene":0,"scenes":[scene],"nodes":node_list,"meshes":meshes,"materials":materials,
    "buffers":[{"byteLength":len(binary)}],"bufferViews":buffer_views,"accessors":accessors,
}
json_payload = json.dumps(gltf, separators=(",",":"), ensure_ascii=False).encode("utf-8")
while len(json_payload) % 4:
    json_payload += b" "
while len(binary) % 4:
    binary.append(0)
total_length = 12 + 8 + len(json_payload) + 8 + len(binary)
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with OUTPUT.open("wb") as handle:
    handle.write(struct.pack("<4sII", b"glTF", 2, total_length))
    handle.write(struct.pack("<I4s", len(json_payload), b"JSON"))
    handle.write(json_payload)
    handle.write(struct.pack("<I4s", len(binary), b"BIN\x00"))
    handle.write(binary)
print(f"Wrote {OUTPUT} ({total_length:,} bytes)")
