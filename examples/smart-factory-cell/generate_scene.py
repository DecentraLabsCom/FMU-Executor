"""Build the self-contained GLB used by the Smart Factory Cell example."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "smart-factory-cell.glb"
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
    accessor = {
        "bufferView": view,
        "componentType": component_type,
        "count": count,
        "type": kind,
    }
    if min_value is not None:
        accessor["min"] = min_value
    if max_value is not None:
        accessor["max"] = max_value
    accessors.append(accessor)
    return len(accessors) - 1


def add_mesh(name: str, faces: list[tuple[list[tuple[float, float, float]], tuple[float, float, float]]], material: int) -> int:
    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    indices: list[int] = []
    for corners, normal in faces:
        start = len(positions)
        positions.extend(corners)
        normals.extend([normal] * len(corners))
        indices.extend((start, start + 1, start + 2, start, start + 2, start + 3))

    position_bytes = b"".join(struct.pack("<3f", *vertex) for vertex in positions)
    normal_bytes = b"".join(struct.pack("<3f", *normal) for normal in normals)
    index_bytes = b"".join(struct.pack("<H", index) for index in indices)
    position_view = add_buffer(position_bytes, 34962)
    normal_view = add_buffer(normal_bytes, 34962)
    index_view = add_buffer(index_bytes, 34963)
    min_value = [min(vertex[axis] for vertex in positions) for axis in range(3)]
    max_value = [max(vertex[axis] for vertex in positions) for axis in range(3)]
    position_accessor = add_accessor(position_view, 5126, len(positions), "VEC3", min_value=min_value, max_value=max_value)
    normal_accessor = add_accessor(normal_view, 5126, len(normals), "VEC3")
    index_accessor = add_accessor(index_view, 5123, len(indices), "SCALAR")
    meshes.append({
        "name": name,
        "primitives": [{
            "attributes": {"POSITION": position_accessor, "NORMAL": normal_accessor},
            "indices": index_accessor,
            "material": material,
            "mode": 4,
        }],
    })
    return len(meshes) - 1


def box_faces() -> list[tuple[list[tuple[float, float, float]], tuple[float, float, float]]]:
    return [
        ([(-.5,-.5,.5),(.5,-.5,.5),(.5,.5,.5),(-.5,.5,.5)], (0,0,1)),
        ([(.5,-.5,-.5),(-.5,-.5,-.5),(-.5,.5,-.5),(.5,.5,-.5)], (0,0,-1)),
        ([(-.5,-.5,-.5),(-.5,-.5,.5),(-.5,.5,.5),(-.5,.5,-.5)], (-1,0,0)),
        ([(.5,-.5,.5),(.5,-.5,-.5),(.5,.5,-.5),(.5,.5,.5)], (1,0,0)),
        ([(-.5,.5,.5),(.5,.5,.5),(.5,.5,-.5),(-.5,.5,-.5)], (0,1,0)),
        ([(-.5,-.5,-.5),(.5,-.5,-.5),(.5,-.5,.5),(-.5,-.5,.5)], (0,-1,0)),
    ]


def cylinder_faces(segments: int = 24):
    faces = []
    for i in range(segments):
        a = 2 * math.pi * i / segments
        b = 2 * math.pi * (i + 1) / segments
        p0 = (.5 * math.cos(a), -.5, .5 * math.sin(a))
        p1 = (.5 * math.cos(b), -.5, .5 * math.sin(b))
        p2 = (.5 * math.cos(b), .5, .5 * math.sin(b))
        p3 = (.5 * math.cos(a), .5, .5 * math.sin(a))
        mid = (math.cos((a + b) / 2), 0, math.sin((a + b) / 2))
        faces.append(([p0, p1, p2, p3], mid))
        faces.append(([(0,-.5,0), p1, p0, (0,-.5,0)], (0,-1,0)))
        faces.append(([(0,.5,0), p3, p2, (0,.5,0)], (0,1,0)))
    return faces


materials = [
    {"name": "Graphite enamel", "pbrMetallicRoughness": {"baseColorFactor": [0.08,0.13,0.17,1], "metallicFactor": .72, "roughnessFactor": .29}},
    {"name": "Safety yellow", "pbrMetallicRoughness": {"baseColorFactor": [1,.55,.06,1], "metallicFactor": .22, "roughnessFactor": .35}},
    {"name": "Cool steel", "pbrMetallicRoughness": {"baseColorFactor": [.36,.47,.53,1], "metallicFactor": .82, "roughnessFactor": .24}},
    {"name": "Conveyor teal", "pbrMetallicRoughness": {"baseColorFactor": [.035,.34,.38,1], "metallicFactor": .27, "roughnessFactor": .42}},
    {"name": "Warm parcel", "pbrMetallicRoughness": {"baseColorFactor": [.92,.34,.12,1], "metallicFactor": .06, "roughnessFactor": .62}},
    {"name": "Pallet blue", "pbrMetallicRoughness": {"baseColorFactor": [.08,.29,.58,1], "metallicFactor": .12, "roughnessFactor": .52}},
    {"name": "Status green", "emissiveFactor": [.08,.8,.28], "pbrMetallicRoughness": {"baseColorFactor": [.05,.55,.16,1], "metallicFactor": .05, "roughnessFactor": .3}},
    {"name": "Status amber", "emissiveFactor": [1,.32,.01], "pbrMetallicRoughness": {"baseColorFactor": [.95,.2,.015,1], "metallicFactor": .05, "roughnessFactor": .3}},
    {"name": "Status red", "emissiveFactor": [.7,.01,.015], "pbrMetallicRoughness": {"baseColorFactor": [.7,.015,.02,1], "metallicFactor": .05, "roughnessFactor": .3}},
    {"name": "Floor", "pbrMetallicRoughness": {"baseColorFactor": [.045,.075,.09,1], "metallicFactor": .35, "roughnessFactor": .78}},
]
boxes = {
    material: add_mesh(f"Box_{material}", box_faces(), material)
    for material in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)
}
rollers = {
    material: add_mesh(f"Roller_{material}", cylinder_faces(), material)
    for material in (2, 6, 7, 8)
}
node_list: list[dict] = []


def add_node(name: str, mesh: int, position, scale, *, rotation=None, parent=None, children=None, extras=None):
    node = {"name": name, "mesh": mesh, "translation": list(position), "scale": list(scale)}
    if rotation:
        node["rotation"] = list(rotation)
    if children:
        node["children"] = children
    if extras:
        node["extras"] = extras
    node_list.append(node)
    return len(node_list) - 1


def add_group(name: str, position, *, rotation=None, children=None, extras=None):
    node = {"name": name, "translation": list(position)}
    if rotation:
        node["rotation"] = list(rotation)
    if children:
        node["children"] = children
    if extras:
        node["extras"] = extras
    node_list.append(node)
    return len(node_list) - 1


# Factory floor, conveyor frame and belt.
floor = add_node("FactoryFloor", boxes[9], (0,-.12,0), (8,.24,5.2))
base = add_node("ConveyorFrame", boxes[0], (0,.48,0), (4.55,.38,1.08))
belt = add_node("ConveyorBelt", boxes[3], (0,.70,0), (4.18,.08,.91), extras={
    "description": "Conveyor driven by the FMI conveyorSpeed input"
})
rail_a = add_node("SafetyRailLeft", boxes[1], (0,.91,-.49), (4.2,.22,.07))
rail_b = add_node("SafetyRailRight", boxes[1], (0,.91,.49), (4.2,.22,.07))
roller_nodes = [add_node(f"Roller_{i+1}", rollers[2], (x,.49,0), (.20,1.05,.20), rotation=(0,0,.70710678, .70710678))
           for i, x in enumerate((-1.75,-.88,0,.88,1.75))]

# A single part is positioned from FMI output productPosition.
carton = add_node("ProductCarton", boxes[4], (-1.6,.91,0), (.42,.39,.42), extras={"fmiBindings": [
    {"variable": "productPosition", "target": "position.x", "scale": 1.0666667, "offset": -1.6},
    {"variable": "productPresent", "target": "visible"},
]})
carton_label = add_node("CartonLabel", boxes[2], (0,.205,.215), (.18,.10,.018))
node_list[carton]["children"] = [carton_label]

# Robot cell: the arm pivot carries the visual binding; the arm geometry is a child.
robot_base = add_node("RobotPedestal", boxes[0], (.72,.85,0), (.70,.48,.68))
robot_shoulder = add_node("RobotShoulder", rollers[2], (.72,1.22,0), (.48,.48,.48))
arm_link = add_node("RobotArmLink", boxes[1], (0,.42,0), (.27,.91,.27))
arm_joint = add_node("RobotElbow", rollers[2], (0,0,0), (.29,.29,.29))
tool = add_node("RobotTool", boxes[3], (0,.22,0), (.42,.15,.30))
tool_group = add_group("RobotToolGroup", (0,0,0), children=[tool])
forearm_group = add_group("RobotForearmGroup", (0,.89,0), children=[arm_joint, tool_group])
arm_group = add_group("RobotArmPivot", (.72,1.25,0), children=[arm_link, forearm_group], extras={"fmiBindings": [
    {"variable": "robotAngle", "target": "rotation.z", "scale": -1, "offset": 0},
]})

# Sorting bins and the three-segment stack light.
reject_bin = add_node("RejectBin", boxes[4], (1.78,.76,-.77), (.76,.55,.68))
good_bin = add_node("GoodPartsBin", boxes[5], (1.78,.76,.77), (.76,.55,.68))
sensor = add_node("PhotoelectricSensor", boxes[2], (-.03,1.12,.66), (.12,.36,.12))
light_pole = add_node("StackLightPole", rollers[2], (-2.27,1.13,-.58), (.10,1.25,.10))
green_light = add_node("StackLightGreen", rollers[6], (-2.27,1.82,-.58), (.25,.23,.25))
amber_light = add_node("StackLightAmber", rollers[7], (-2.27,2.10,-.58), (.25,.23,.25))
red_light = add_node("StackLightRed", rollers[8], (-2.27,2.38,-.58), (.25,.23,.25), extras={"fmiBindings": [
    {"variable": "alarm", "target": "material.color", "falseColor": "#cc2435", "trueColor": "#ff1628"},
]})
guard = add_node("SafetyGuard", boxes[1], (1.65,1.28,.90), (.08,1.35,.08))
diverter = add_node("SorterDiverter", boxes[1], (.55,1.02,0), (.10,.35,.78), extras={"fmiBindings": [
    {"variable": "sortOffset", "target": "rotation.y", "scale": 1, "offset": 0},
]})

# Root nodes are listed explicitly; the groups above are not children of a root.
roots = [floor, base, belt, rail_a, rail_b, *roller_nodes, carton,
         robot_base, robot_shoulder, arm_group, reject_bin, good_bin, sensor,
         light_pole, green_light, amber_light, red_light, guard, diverter]
scene = {"name": "Smart Factory Twin Cell", "nodes": roots}

gltf = {
    "asset": {"version": "2.0", "generator": "DecentraLabs Smart Factory Cell"},
    "scene": 0,
    "scenes": [scene],
    "nodes": node_list,
    "meshes": meshes,
    "materials": materials,
    "buffers": [{"byteLength": 0}],
    "bufferViews": buffer_views,
    "accessors": accessors,
}
gltf["buffers"][0]["byteLength"] = len(binary)
json_payload = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
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
