"""Build the self-contained GLB for the Smart Warehouse Cell example."""

from __future__ import annotations

import json
import math
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "smart-warehouse-cell.glb"
OUTBOUND_TRUCK_X = 4.9
CARRIER_WHEEL_RADIUS = .155
SOURCE_PACKAGE_X = (-4.0, -3.5, -3.0, -1.7, -1.2, -0.7)
PACKAGE_SIZES = (
    (.48, .42, .46), (.60, .54, .40), (.42, .36, .64),
    (.56, .60, .50), (.60, .48, .62), (.50, .56, .58),
)
PACKAGE_MATERIALS = (5, 6, 4)
PACKAGE_COLOR_BINDINGS = (
    ("Blue", "packageColorBlue"),
    ("Gold", "packageColorGold"),
    ("Coral", "packageColorCoral"),
)
binary = bytearray()
buffer_views: list[dict] = []
accessors: list[dict] = []
meshes: list[dict] = []


def add_buffer(payload: bytes, target: int | None = None) -> int:
    while len(binary) % 4:
        binary.append(0)
    offset = len(binary)
    binary.extend(payload)
    view = {"buffer": 0, "byteOffset": offset, "byteLength": len(payload)}
    if target is not None:
        view["target"] = target
    buffer_views.append(view)
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


def cylinder_faces(segments: int = 32):
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


def cylinder_faces_x(segments: int = 32):
    """Return cylinder faces with the shaft along local X for roller animation."""
    faces = []
    for corners, normal in cylinder_faces(segments):
        rotated_corners = [(-y, x, z) for x, y, z in corners]
        rotated_normal = (-normal[1], normal[0], normal[2])
        faces.append((rotated_corners, rotated_normal))
    return faces


def _write_mesh(name: str, material: int, positions, normals, texcoords, indices) -> int:
    p_view = add_buffer(b"".join(struct.pack("<3f", *v) for v in positions), 34962)
    n_view = add_buffer(b"".join(struct.pack("<3f", *v) for v in normals), 34962)
    uv_view = add_buffer(b"".join(struct.pack("<2f", *v) for v in texcoords), 34962)
    i_view = add_buffer(b"".join(struct.pack("<H", value) for value in indices), 34963)
    p_accessor = add_accessor(p_view, 5126, len(positions), "VEC3",
                              min_value=[min(v[i] for v in positions) for i in range(3)],
                              max_value=[max(v[i] for v in positions) for i in range(3)])
    n_accessor = add_accessor(n_view, 5126, len(normals), "VEC3")
    uv_accessor = add_accessor(uv_view, 5126, len(texcoords), "VEC2")
    i_accessor = add_accessor(i_view, 5123, len(indices), "SCALAR")
    meshes.append({"name": name, "primitives": [{
        "attributes": {"POSITION": p_accessor, "NORMAL": n_accessor, "TEXCOORD_0": uv_accessor},
        "indices": i_accessor, "material": material, "mode": 4,
    }]})
    return len(meshes) - 1


def add_mesh(name: str, material: int, faces=None, *, smooth_cylinder_axis: str | None = None) -> int:
    positions = []
    normals = []
    texcoords = []
    indices = []
    for corners, normal in faces or box_faces():
        start = len(positions)
        positions.extend(corners)
        if smooth_cylinder_axis == "y":
            for x, y, z in corners:
                length = math.hypot(x, z)
                normals.append(normal if abs(normal[1]) > .5 or length < 1e-8 else (x / length, 0, z / length))
        elif smooth_cylinder_axis == "x":
            for x, y, z in corners:
                length = math.hypot(y, z)
                normals.append(normal if abs(normal[0]) > .5 or length < 1e-8 else (0, y / length, z / length))
        else:
            normals.extend([normal] * len(corners))
        texcoords.extend(((0, 0), (1, 0), (1, 1), (0, 1)))
        indices.extend((start, start + 1, start + 2, start, start + 2, start + 3))
    return _write_mesh(name, material, positions, normals, texcoords, indices)


def add_rounded_box_mesh(name: str, material: int, *, bevel: float = .075, divisions: int = 16) -> int:
    """Create a softly rounded box with continuous normals and face UVs."""
    positions = []
    normals = []
    texcoords = []
    indices = []

    def interpolate(a, b, c, d, u, v):
        return tuple(
            (1 - v) * ((1 - u) * a[axis] + u * b[axis])
            + v * ((1 - u) * d[axis] + u * c[axis])
            for axis in range(3)
        )

    def round_point(point, face_normal):
        core = tuple(max(-.5 + bevel, min(.5 - bevel, value)) for value in point)
        delta = tuple(point[axis] - core[axis] for axis in range(3))
        length = math.sqrt(sum(value * value for value in delta))
        if length < 1e-8:
            return point, face_normal
        normal = tuple(value / length for value in delta)
        return tuple(core[axis] + normal[axis] * bevel for axis in range(3)), normal

    for corners, face_normal in box_faces():
        grid = []
        for y in range(divisions + 1):
            v = y / divisions
            row = []
            for x in range(divisions + 1):
                u = x / divisions
                point = interpolate(*corners, u, v)
                row.append(round_point(point, face_normal))
            grid.append(row)
        for y in range(divisions):
            for x in range(divisions):
                start = len(positions)
                for rounded, uv in (
                    (grid[y][x], (x / divisions, y / divisions)),
                    (grid[y][x + 1], ((x + 1) / divisions, y / divisions)),
                    (grid[y + 1][x + 1], ((x + 1) / divisions, (y + 1) / divisions)),
                    (grid[y + 1][x], (x / divisions, (y + 1) / divisions)),
                ):
                    positions.append(rounded[0])
                    normals.append(rounded[1])
                    texcoords.append(uv)
                indices.extend((start, start + 1, start + 2, start, start + 2, start + 3))
    return _write_mesh(name, material, positions, normals, texcoords, indices)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)


def make_surface_texture(surface: str, size: int = 256) -> bytes:
    """Create a subtle, repeatable color texture without external asset dependencies."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter: none
        for x in range(size):
            grain = (((x * 374761393 + y * 668265263 + 1442695041) & 0xffffffff) >> 24) / 255 - .5
            if surface == "cardboard":
                fiber = math.sin(y * .92 + math.sin(x * .07) * .8) * .025
                fleck = math.sin(x * 1.71 + y * .37) * math.sin(y * .53) * .018
                value = 0.94 + fiber + fleck + grain * .045
            elif surface == "steel":
                brush = math.sin(y * 2.1 + math.sin(x * .11)) * .025
                value = 0.94 + brush + grain * .035
            elif surface == "asphalt":
                pock = math.sin(x * .22 + math.sin(y * .16) * 2) * math.sin(y * .19) * .045
                value = 0.91 + pock + grain * .055
            else:
                cloud = math.sin(x * .12 + math.sin(y * .09) * 1.2) * math.sin(y * .14) * .035
                value = 0.94 + cloud + grain * .035
            channel = max(0, min(255, round(value * 255)))
            rows.extend((channel, channel, channel))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _png_chunk(b"IEND", b"")


materials = [
    {"name":"Obsidian frame","pbrMetallicRoughness":{"baseColorFactor":[.055,.095,.13,1],"metallicFactor":.46,"roughnessFactor":.43}},
    {"name":"Safety amber","pbrMetallicRoughness":{"baseColorFactor":[1,.54,.08,1],"metallicFactor":.08,"roughnessFactor":.48}},
    {"name":"Brushed alloy","pbrMetallicRoughness":{"baseColorFactor":[.48,.60,.67,1],"metallicFactor":.56,"roughnessFactor":.39}},
    {"name":"Aisle teal","pbrMetallicRoughness":{"baseColorFactor":[.03,.49,.51,1],"metallicFactor":.06,"roughnessFactor":.55}},
    {"name":"Parcel coral","pbrMetallicRoughness":{"baseColorFactor":[.96,.31,.16,1],"metallicFactor":.05,"roughnessFactor":.55}},
    {"name":"Parcel blue","pbrMetallicRoughness":{"baseColorFactor":[.12,.38,.82,1],"metallicFactor":.12,"roughnessFactor":.42}},
    {"name":"Parcel gold","pbrMetallicRoughness":{"baseColorFactor":[1,.72,.13,1],"metallicFactor":.08,"roughnessFactor":.46}},
    {"name":"Pallet mint","pbrMetallicRoughness":{"baseColorFactor":[.18,.78,.60,1],"metallicFactor":.18,"roughnessFactor":.36}},
    {"name":"Status lamp","emissiveFactor":[.06,.10,.10],"pbrMetallicRoughness":{"baseColorFactor":[.24,.35,.36,1],"metallicFactor":.06,"roughnessFactor":.25}},
    {"name":"Floor","pbrMetallicRoughness":{"baseColorFactor":[.045,.075,.09,1],"metallicFactor":.04,"roughnessFactor":.91}},
    {"name":"Glass cyan","emissiveFactor":[.02,.4,.45],"pbrMetallicRoughness":{"baseColorFactor":[.04,.42,.48,1],"metallicFactor":.38,"roughnessFactor":.25}},
    {"name":"Carbon black","pbrMetallicRoughness":{"baseColorFactor":[.018,.027,.034,1],"metallicFactor":.24,"roughnessFactor":.62}},
    {"name":"Courier blue","pbrMetallicRoughness":{"baseColorFactor":[.045,.20,.36,1],"metallicFactor":.12,"roughnessFactor":.5}},
    {"name":"Fleet white","pbrMetallicRoughness":{"baseColorFactor":[.78,.85,.84,1],"metallicFactor":.06,"roughnessFactor":.52}},
    {"name":"Safety glass","emissiveFactor":[.025,.10,.13],"pbrMetallicRoughness":{"baseColorFactor":[.055,.25,.29,1],"metallicFactor":.44,"roughnessFactor":.2}},
    {"name":"Concrete trim","pbrMetallicRoughness":{"baseColorFactor":[.13,.19,.22,1],"metallicFactor":.04,"roughnessFactor":.84}},
    {"name":"Signal cyan","emissiveFactor":[.04,.85,.77],"pbrMetallicRoughness":{"baseColorFactor":[.08,.80,.69,1],"metallicFactor":.18,"roughnessFactor":.24}},
    {"name":"Signal amber","emissiveFactor":[.78,.31,.035],"pbrMetallicRoughness":{"baseColorFactor":[1,.39,.035,1],"metallicFactor":.12,"roughnessFactor":.25}},
    {"name":"Road graphite","pbrMetallicRoughness":{"baseColorFactor":[.025,.04,.05,1],"metallicFactor":.015,"roughnessFactor":.96}},
    {"name":"Tire rubber","pbrMetallicRoughness":{"baseColorFactor":[.014,.018,.021,1],"metallicFactor":0,"roughnessFactor":.94}},
]
for index in (4, 5, 6):
    materials[index]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
materials[2]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 1}
for index in (9, 15):
    materials[index]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 2}
materials[18]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 3}
box_meshes = [add_mesh(f"Box_{index}", index) for index in range(len(materials))]
rounded_box_meshes = {
    index: add_rounded_box_mesh(f"RoundedBox_{index}", index, bevel=.055)
    for index in (4, 5, 6, 12, 13)
}
wheel_mesh = add_mesh("TireRubber", 19, cylinder_faces(), smooth_cylinder_axis="y")
belt_roller_mesh = add_mesh("BeltRoller", 2, cylinder_faces_x(), smooth_cylinder_axis="x")
inbound_roller_mesh = add_mesh("InboundConveyorRoller", 2, cylinder_faces(), smooth_cylinder_axis="y")
carrier_wheel_hub_mesh = add_mesh("CarrierCartWheelHub", 2, cylinder_faces(), smooth_cylinder_axis="y")
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


def add_group(name: str, position, *, rotation=None, children=None, extras=None):
    node = {"name": name, "translation": list(position)}
    if rotation is not None:
        node["rotation"] = list(rotation)
    if children:
        node["children"] = children
    if extras:
        node["extras"] = extras
    node_list.append(node)
    return len(node_list) - 1


def add_colored_package(name: str, visibility_variable: str, *, position=(0, 0, 0), agv=False, transfer=False):
    children = []
    for material_index, (color_name, color_variable) in zip(PACKAGE_MATERIALS, PACKAGE_COLOR_BINDINGS):
        bindings = [
            {"variable":color_variable,"target":"visible"},
            {"variable":"packageSizeX","target":"scale.x"},
            {"variable":"packageSizeY","target":"scale.y"},
            {"variable":"packageSizeZ","target":"scale.z"},
        ]
        if agv:
            bindings.append({"variable":"packageSizeY","target":"position.y","scale":.5,"offset":.585})
        if transfer:
            bindings.extend([
                {"variable":"transferPackageX","target":"position.x"},
                {"variable":"transferPackageY","target":"position.y"},
                {"variable":"transferPackageZ","target":"position.z"},
            ])
        children.append(add_node(
            f"{name}{color_name}",
            rounded_box_meshes[material_index],
            position,
            PACKAGE_SIZES[0],
            extras={"fmiBindings":bindings},
        ))
    return add_group(name, (0, 0, 0), children=children, extras={"fmiBindings":[
        {"variable":visibility_variable,"target":"visible"},
    ]})


roots = []
roots.append(add_node("WarehouseFoundation", box_meshes[15], (0,-.34,1.9), (18,.42,20.7)))
roots.append(add_node("WarehouseFloor", box_meshes[9], (0,-.13,0), (17.5,.26,14.6)))
roots.append(add_node("LoadingApron", box_meshes[18], (0,-.11,9.1), (18,.22,4.5)))
roots.append(add_node("CentralAisle", box_meshes[3], (0,.006,0), (10.8,.015,1.48)))
for z in (-.83,.83):
    roots.append(add_node(f"AisleSafetyLine_{z}", box_meshes[1], (0,.018,z), (10.6,.025,.055)))
for x in range(-5, 6):
    roots.append(add_node(f"FloorGuide_{x}", box_meshes[2], (x,.003,0), (.018,.008,8.6)))

# A broad dock apron, traffic markings, bumpers and painted pedestrian lanes.
for x in (-7.3, -5.4, 5.4, 7.3):
    roots.append(add_node(f"ApronLane_{x}", box_meshes[1], (x,.012,8.95), (.045,.018,3.8)))
for z in (7.1, 10.9):
    roots.append(add_node(f"ApronStopLine_{z}", box_meshes[2], (0,.015,z), (15,.025,.055)))
for x in (-7.4, -2.3, 2.3, 7.4):
    roots.append(add_node(f"DockBollard_{x}", box_meshes[1], (x,.52,5.72), (.20,1.04,.24)))

# Open industrial shell and roof trusses add scale without hiding the working cell.
for x in (-8.25, 8.25):
    for z in (-6.1, -2.1, 2.0, 5.5):
        roots.append(add_node(f"ShellColumn_{x}_{z}", box_meshes[0], (x,3.25,z), (.28,6.5,.28)))
    roots.append(add_node(f"ShellRunner_{x}", box_meshes[15], (x,6.4,-.2), (.42,.30,12.1)))
for z in (-6.1, -2.1, 2.0, 5.5):
    roots.append(add_node(f"RoofCrossbeam_{z}", box_meshes[15], (0,6.38,z), (16.7,.25,.28)))
    roots.append(add_node(f"RoofLight_{z}", box_meshes[16], (0,6.18,z-.62), (5.2,.055,.14)))
for x in (-6.7, -2.3, 2.3, 6.7):
    roots.append(add_node(f"RoofBrace_{x}", box_meshes[0], (x,6.63,-.2), (.12,.12,12.4)))

# Loading docks: framed shutter bays, dock lights, and striped impact rails.
for bay, x in enumerate((-4.9, 4.9), start=1):
    roots.append(add_node(f"DockOpening_{bay}_header", box_meshes[0], (x,5.48,5.46), (3.75,.42,.35)))
    for side in (-1, 1):
        roots.append(add_node(f"DockOpening_{bay}_jamb_{side}", box_meshes[2], (x+side*1.72,3.38,5.46), (.26,4.0,.33)))
        roots.append(add_node(f"DockGuide_{bay}_{side}", box_meshes[1], (x+side*1.47,3.25,5.23), (.08,3.25,.08)))
    if bay == 1:
        for y in (2.0, 2.35, 2.7, 3.05, 3.4, 3.75, 4.1):
            roots.append(add_node(f"ShutterSlat_{bay}_{y}", box_meshes[15], (x,y,5.49), (3.12,.075,.12)))
    roots.append(add_node(f"DockCanopy_{bay}", box_meshes[2], (x,5.0,6.1), (4.2,.16,1.15)))
    roots.append(add_node(f"DockSignal_{bay}", box_meshes[16], (x-1.43,4.78,5.2), (.20,.16,.12)))
    if bay == 1:
        roots.append(add_node(f"DockBumper_{bay}", box_meshes[12], (x,1.1,5.27), (3.05,.25,.25)))
    else:
        # Leave the centered conveyor and package path clear at the outbound bay.
        for side in (-1, 1):
            roots.append(add_node(f"DockBumper_{bay}_{side}", box_meshes[12], (x+side*1.55,1.1,5.27), (.28,.25,.25)))

def add_truck(name: str, x: float, z: float, body_material: int, *, open_loading_door: bool = False):
    """Add a detailed static delivery truck as dock-side scene dressing."""
    children = []
    children.append(add_node(f"{name}_chassis", box_meshes[11], (0,.68,0), (2.32,.34,4.65)))
    if open_loading_door:
        # Keep the rear open so the loader and the six model-driven parcels are visible.
        children.append(add_node(f"{name}_trailer_floor", box_meshes[body_material], (0,.79,-.72), (2.62,.12,3.08)))
        children.append(add_node(f"{name}_trailer_front", box_meshes[body_material], (0,2.0,.82), (2.72,2.55,.12)))
        for side in (-1, 1):
            children.append(add_node(f"{name}_trailer_side_{side}", box_meshes[body_material], (side*1.31,2.0,-.72), (.10,2.48,3.08)))
    else:
        children.append(add_node(f"{name}_trailer", box_meshes[body_material], (0,2.0,-.72), (2.72,2.55,3.18)))
    children.append(add_node(f"{name}_trailer_roof", box_meshes[13], (0,3.31,-.72), (2.78,.13,3.24)))
    if not open_loading_door:
        children.append(add_node(f"{name}_trailer_rear", box_meshes[2], (0,2.02,-2.34), (2.76,2.62,.13)))
    children.append(add_node(f"{name}_cab", rounded_box_meshes[12], (0,1.25,1.92), (2.16,1.55,1.72)))
    children.append(add_node(f"{name}_cab_roof", rounded_box_meshes[13], (0,2.08,1.94), (2.1,.20,1.68)))
    children.append(add_node(f"{name}_windscreen", box_meshes[14], (0,1.73,2.79), (1.55,.61,.055)))
    children.append(add_node(f"{name}_bumper", box_meshes[2], (0,.69,2.83), (2.30,.24,.22)))
    for side in (-1, 1):
        children.append(add_node(f"{name}_mirror_{side}", box_meshes[2], (side*1.18,1.56,2.46), (.13,.13,.36)))
        children.append(add_node(f"{name}_headlamp_{side}", box_meshes[16], (side*.76,1.0,2.84), (.30,.22,.06)))
        children.append(add_node(f"{name}_side_stripe_{side}", box_meshes[16], (side*1.38,1.45,-.70), (.055,.10,3.12)))
        for wheel_z in (-2.0, -1.35, .78, 2.03):
            children.append(add_node(f"{name}_wheel_{side}_{wheel_z}", wheel_mesh, (side*1.34,.42,wheel_z), (.76,.27,.76), rotation=(0,0,.70710678,.70710678)))
    for side in (-1, 1):
        for rib_z in (-1.9, -1.45, -1.0, -.55, -.1, .35, .8):
            children.append(add_node(f"{name}_trailer_rib_{side}_{rib_z}", box_meshes[2], (side*1.37,2.0,rib_z), (.055,2.4,.045)))
    if open_loading_door:
        for index in range(6):
            slot_x = (0.0, -.70, .70)[index % 3]
            slot_z = (-.58, -1.58)[index // 3]
            package_size = PACKAGE_SIZES[index]
            children.append(add_node(
                f"TruckCargo_{index + 1}",
                rounded_box_meshes[PACKAGE_MATERIALS[index % 3]],
                (slot_x,.85 + package_size[1] * .5,slot_z),
                package_size,
                extras={"fmiBindings":[{
                    "variable":"truckLoadCount",
                    "target":"visible",
                    "visibleWhenGreaterThanOrEqual":index + 1,
                }]},
            ))
    roots.append(add_group(name, (x,0,z), children=children))

add_truck("InboundDeliveryTruck", -4.9, 7.85, 13)
add_truck("OutboundDeliveryTruck", 4.9, 7.85, 12, open_loading_door=True)

# Two high-bay pallet racks frame a broad, open AGV aisle. The rack beside the
# loading docks is narrower so it stays entirely between the two parked trucks.
for row, z in enumerate((-3.55, 3.55)):
    rack_x_positions = (-4.65,-2.35,-.05,2.25,4.55) if row == 0 else (-2.4,-1.2,0.0,1.2,2.4)
    stock_x_positions = (-3.5,-1.2,1.1,3.4) if row == 0 else (-1.8,-.6,.6,1.8)
    rack_width = 9.35 if row == 0 else 5.0
    brace_length = 4.15 if row == 0 else 1.65
    brace_angle = .988 if row == 0 else .82
    for x in rack_x_positions:
        roots.append(add_node(f"Rack_{row}_{x}_upright_a", box_meshes[0], (x,2.0,z), (.13,4.0,.16)))
        roots.append(add_node(f"Rack_{row}_{x}_upright_b", box_meshes[0], (x,2.0,z + (0.92 if row else -.92)), (.13,4.0,.16)))
    for shelf_y in (.45,1.72,2.98,4.0):
        roots.append(add_node(f"Rack_{row}_beam_{shelf_y}", box_meshes[1], (0,shelf_y,z), (rack_width,.12,.2)))
    for x in stock_x_positions:
        for level, y in enumerate((.98,2.24,3.52)):
            depth_sign = -1 if row else 1
            pallet_width = 1.58 if row == 0 else 1.3
            pallet = add_node(f"Pallet_{row}_{x}_{level}", box_meshes[7], (x,y-.29,z + depth_sign*.47), (pallet_width,.12,.74))
            roots.append(pallet)
            palette = (4,5,6)[(int(x*10) + level + row) % 3]
            selected_package_pallet = row == 0 and level == 1 and x in (-3.5, -1.2)
            if not selected_package_pallet:
                roots.append(add_node(f"StockCarton_{row}_{x}_{level}", rounded_box_meshes[palette], (x,y,z + depth_sign*.47), (1.15,.54,.58)))
            roots.append(add_node(f"StockLabel_{row}_{x}_{level}", box_meshes[2], (x,y+.03,z + depth_sign*.17), (.38,.16,.035)))
    # Cross braces and end-of-aisle guards make each rack bay read as real steelwork.
    frame_z = z + (-0.86 if row else 0.86)
    for x in stock_x_positions:
        roots.append(add_node(f"RackBrace_{row}_{x}_up", box_meshes[2], (x,2.0,frame_z), (brace_length,.065,.07), rotation=(0,0,math.sin(brace_angle/2),math.cos(brace_angle/2))))
        roots.append(add_node(f"RackBrace_{row}_{x}_down", box_meshes[2], (x,2.0,frame_z-.025), (brace_length,.065,.07), rotation=(0,0,-math.sin(brace_angle/2),math.cos(brace_angle/2))))
    end_guard_x = (-5.05, 5.05) if row == 0 else (-2.7, 2.7)
    for x in end_guard_x:
        roots.append(add_node(f"RackEndGuard_{row}_{x}", box_meshes[1], (x,.56,z), (.28,1.12,.32)))

# Six individually tracked parcels occupy two shelf pallets. Each box is small
# enough for the AGV deck, the loading belt and the truck cargo slots.
for index, (package_x, package_size) in enumerate(zip(SOURCE_PACKAGE_X, PACKAGE_SIZES), start=1):
    roots.append(add_node(
        f"ShelfPackage_{index}", rounded_box_meshes[PACKAGE_MATERIALS[(index - 1) % 3]],
        (package_x,2.01 + package_size[1] * .5,-3.08), package_size,
        extras={"fmiBindings":[{"variable":f"shelfPackage{index}Visible","target":"visible"}]},
    ))

# The warehouse collection belt, rollers and sort gate at the rear.
roots.append(add_node("InboundConveyorFrame", box_meshes[0], (0,.55,-4.62), (7.4,.44,.92)))
roots.append(add_node("InboundConveyorBelt", box_meshes[3], (0,.80,-4.62), (7.15,.08,.78)))
for x in (-3.2,-2.4,-1.6,-.8,0,.8,1.6,2.4,3.2):
    roots.append(add_node(f"ConveyorRoller_{x}", inbound_roller_mesh, (x,.55,-4.62), (.20,.98,.20), rotation=(0,0,.70710678,.70710678)))
roots.append(add_node("SortGate", box_meshes[1], (2.6,1.05,-4.62), (.18,.48,.95)))
roots.append(add_node("InboundParcel", rounded_box_meshes[4], (-2.6,1.06,-4.62), (.72,.50,.62)))

# The low-profile carrier cart has a four-wheel chassis and a raised roller deck
# that reaches toward the outbound belt for a clean parcel handoff.
agv_children = [
    add_node("CarrierCartChassis", rounded_box_meshes[12], (0,.29,0), (1.50,.30,1.04)),
    add_node("CarrierCartUndercarriage", box_meshes[0], (0,.13,0), (1.30,.10,.82)),
    add_node("CarrierCartFrontPanel", box_meshes[3], (.77,.31,0), (.08,.21,.76)),
    add_node("CarrierCartRearPanel", rounded_box_meshes[13], (-.77,.31,0), (.08,.21,.76)),
    add_node("CarrierCartFrontLampLeft", box_meshes[16], (.82,.36,-.23), (.025,.075,.11)),
    add_node("CarrierCartFrontLampRight", box_meshes[16], (.82,.36,.23), (.025,.075,.11)),
    add_node("CarrierCartRearReflectorLeft", box_meshes[1], (-.82,.34,-.23), (.025,.055,.11)),
    add_node("CarrierCartRearReflectorRight", box_meshes[1], (-.82,.34,.23), (.025,.055,.11)),
]
for x, x_label in ((-.54, "Rear"), (.54, "Front")):
    for z, side_label in ((-.55, "Left"), (.55, "Right")):
        # Cylinders use local Y as their axle. The fixed parent turns that axle
        # onto world Z, and the child spins around local Y. Forward travel is
        # along +X, so rolling contact requires a negative rotation about +Z.
        wheel_spin_children = [
            add_node(
                f"CarrierCartWheelTire_{x_label}_{side_label}", wheel_mesh,
                (0,0,0), (.31,.18,.31),
            ),
            add_node(
                f"CarrierCartWheelHub_{x_label}_{side_label}", carrier_wheel_hub_mesh,
                (0,.10 if z > 0 else -.10,0), (.12,.045,.12),
            ),
        ]
        wheel_spin = add_group(
            f"CarrierCartWheelSpin_{x_label}_{side_label}", (0,0,0),
            children=wheel_spin_children,
            extras={"fmiBindings":[{
                "variable":"agvPosition","target":"rotation.y",
                "scale":-1 / CARRIER_WHEEL_RADIUS,
            }]},
        )
        agv_children.append(add_group(
            f"CarrierCartWheel_{x_label}_{side_label}", (x,.16,z),
            rotation=(.70710678,0,0,.70710678),
            children=[wheel_spin],
        ))

carrier_deck_children = [
    add_node("CarrierCartDeckPlate", box_meshes[2], (0,0,0), (1.34,.08,1.32)),
    add_node("CarrierCartDeckSupportLeft", box_meshes[12], (-.53,-.13,-.20), (.10,.18,1.0)),
    add_node("CarrierCartDeckSupportRight", box_meshes[12], (.53,-.13,-.20), (.10,.18,1.0)),
]
for index, x in enumerate((-.50,-.30,-.10,.10,.30,.50), start=1):
    carrier_deck_children.append(add_node(
        f"CarrierCartRoller_{index}", belt_roller_mesh,
        (x,.085,0), (1.12,.08,.08), rotation=(0,.70710678,0,.70710678),
    ))
for z, side_label in ((-.60, "Left"), (.60, "Right")):
    carrier_deck_children.append(add_node(
        f"CarrierCartCargoGuide_{side_label}", box_meshes[1],
        (0,.10,z), (1.24,.14,.055),
    ))
for x, end_label in ((-.62, "Rear"), (.62, "Front")):
    carrier_deck_children.append(add_node(
        f"CarrierCartCargoStop_{end_label}", box_meshes[1],
        (x,.10,0), (.055,.14,1.18),
    ))
agv_children.append(add_group("CarrierCartCargoDeck", (0,.46,.85), children=carrier_deck_children, extras={"fmiBindings":[
    {"variable":"forkHeight","target":"position.y","scale":1,"offset":0},
]}))
agv_children.append(add_colored_package("WarehousePackage", "packageOnAgv", position=(0,.86,.85), agv=True))
agv_group = add_group("WarehouseCarrierCart", (-3.5,0,-.12), children=agv_children, extras={"fmiBindings":[
    {"variable":"agvPosition","target":"position.x","scale":1,"offset":-3.5},
]})
roots.append(agv_group)

# The trolley runs along an overhead rail to the selected shelf parcel. It lifts
# the package above the rack, returns to the AGV handoff point, then lowers it.
pickup_arm_children = [
    add_node("PickArmCarriage", box_meshes[1], (0,-.12,0), (.72,.22,.56)),
    add_node("PickArmShoulder", box_meshes[2], (0,-.30,0), (.48,.18,.46)),
    add_node("PickArmReach", box_meshes[1], (0,-.30,0), (.24,.16,0), extras={"fmiBindings":[
        {"variable":"pickupArmReachProgress","target":"position.z","scale":.5},
        {"variable":"pickupArmReachProgress","target":"scale.z"},
    ]}),
    add_node("PickArmDropRod", box_meshes[2], (0,0,0), (.11,0,.11), extras={"fmiBindings":[
        {"variable":"pickupDropProgress","target":"position.y","scale":.5,"offset":-.15},
        {"variable":"pickupArmReachProgress","target":"position.z"},
        {"variable":"pickupDropProgress","target":"scale.y","scale":-1,"offset":-.3},
    ]}),
]
end_effector_children = [
    add_node("PickArmTool", box_meshes[2], (0,.30,0), (.52,.12,.42)),
    add_node("PickArmGripperLeft", box_meshes[16], (-.50,0,0), (.08,.48,.12), extras={"fmiBindings":[
        {"variable":"pickupGripperGap","target":"position.x","scale":-.5},
    ]}),
    add_node("PickArmGripperRight", box_meshes[16], (.50,0,0), (.08,.48,.12), extras={"fmiBindings":[
        {"variable":"pickupGripperGap","target":"position.x","scale":.5},
    ]}),
]
claw_group = add_group("PickArmClaw", (0,0,0), children=end_effector_children, extras={"fmiBindings":[
    {"variable":"robotAngle","target":"rotation.x","scale":1,"offset":0},
]})
pickup_package = add_colored_package("PickTransferPackage", "pickupPackageVisible")
end_effector = add_group("PickArmEndEffector", (0,0,0), children=[claw_group,pickup_package], extras={"fmiBindings":[
    {"variable":"pickupDropProgress","target":"position.y"},
    {"variable":"pickupArmReachProgress","target":"position.z"},
]})
pickup_arm_children.append(end_effector)
roots.append(add_group("PickArmPivot", (-3.5,3.3,-3.08), children=pickup_arm_children, extras={"fmiBindings":[
    {"variable":"pickupArmX","target":"position.x"},
    {"variable":"pickupArmY","target":"position.y"},
    {"variable":"pickupArmZ","target":"position.z"},
    {"variable":"pickupArmAngle","target":"rotation.y"},
]}))

# A powered roller conveyor hands each AGV parcel to a loader arm at the truck's open rear.
roots.append(add_node("OutboundTransferConveyorFrame", box_meshes[0], (OUTBOUND_TRUCK_X,.51,3.03), (1.08,.28,4.86)))
roots.append(add_node("OutboundTransferConveyor", box_meshes[3], (OUTBOUND_TRUCK_X,.69,3.03), (.94,.08,4.78)))
for index in range(18):
    z = .78 + index * .27
    roots.append(add_node(f"OutboundTransferRoller_{index + 1}", belt_roller_mesh, (OUTBOUND_TRUCK_X,.64,z), (.92,.10,.10), extras={"fmiBindings":[
        {"variable":"beltPhase","target":"rotation.x","scale":1,"offset":0},
    ]}))
for offset in (-.52,.52):
    x = OUTBOUND_TRUCK_X + offset
    roots.append(add_node(f"OutboundConveyorRail_{x}", box_meshes[2], (x,.83,3.405), (.07,.25,4.15)))

roots.append(add_node("TruckLoaderBase", box_meshes[0], (OUTBOUND_TRUCK_X,.72,5.12), (.72,.28,.70)))
roots.append(add_node("TruckLoaderColumn", box_meshes[2], (OUTBOUND_TRUCK_X,1.27,5.12), (.22,.92,.22)))
loader_children = [
    add_node("TruckLoaderShoulder", box_meshes[16], (0,0,0), (.46,.42,.46)),
    add_node("TruckLoaderTelescopicBoom", box_meshes[1], (0,0,0), (.22,.18,0), extras={"fmiBindings":[
        {"variable":"loaderReach","target":"position.z","scale":.5,"offset":0},
        {"variable":"loaderReach","target":"scale.z","scale":1,"offset":0},
    ]}),
]
loader_gripper_children = [
    add_node("TruckLoaderGripper", box_meshes[2], (0,0,0), (.48,.34,.20)),
    add_node("TruckLoaderFingerLeft", box_meshes[16], (-.22,-.18,.11), (.10,.30,.12)),
    add_node("TruckLoaderFingerRight", box_meshes[16], (.22,-.18,.11), (.10,.30,.12)),
]
loader_children.append(add_group("TruckLoaderEndEffector", (0,-.04,0), children=loader_gripper_children, extras={"fmiBindings":[
    {"variable":"loaderReach","target":"position.z","scale":1,"offset":0},
]}))
roots.append(add_group("TruckLoaderArm", (OUTBOUND_TRUCK_X,1.54,5.12), children=loader_children, extras={"fmiBindings":[
    {"variable":"loaderAngle","target":"rotation.y","scale":1,"offset":0},
]}))
roots.append(add_colored_package(
    "TruckTransferPackage",
    "transferPackageVisible",
    position=(OUTBOUND_TRUCK_X,.88,.73),
    transfer=True,
))

roots.append(add_node("PickCellBase", box_meshes[0], (-3.5,.72,-3.78), (1.0,.45,.9)))
for side, x in (("Left",-4.55),("Right",0.0)):
    roots.append(add_node(f"PickArmRailColumn{side}", box_meshes[2], (x,1.63,-3.08), (.22,3.26,.22)))
roots.append(add_node("PickArmRail", box_meshes[0], (-2.275,3.43,-3.08), (4.55,.16,.34)))
roots.append(add_node("PickArmRailGuide", box_meshes[2], (-2.275,3.32,-3.08), (4.35,.07,.20)))
roots.append(add_node("WarehouseAlarmBase", box_meshes[0], (4.95,.15,-1.78), (.58,.30,.58)))
roots.append(add_node("WarehouseAlarmLamp", box_meshes[8], (4.95,.66,-1.78), (.34,.72,.34), extras={"fmiBindings":[
    {"variable":"alarm","target":"material.color","falseColor":"#31dfa0","trueColor":"#ff365f"},
]}))
roots.append(add_node("WarehouseInfoPanel", box_meshes[10], (0,3.45,-4.30), (2.2,1.05,.09)))
for x in (-.78,-.45,-.12,.21,.54,.87):
    roots.append(add_node(f"PanelSignal_{x}", box_meshes[6], (x,3.45,-4.23), (.18,.18,.035)))

scene = {"name":"Smart Warehouse Twin", "nodes":roots}
surface_kinds = ("cardboard", "steel", "concrete", "asphalt")
surface_pngs = [make_surface_texture(kind) for kind in surface_kinds]
surface_images = [
    {"name":f"{kind.replace('-', ' ').title()} surface", "bufferView":add_buffer(payload), "mimeType":"image/png"}
    for kind, payload in zip(("cardboard", "brushed-steel", "polished-concrete", "dock-asphalt"), surface_pngs)
]
gltf = {
    "asset":{"version":"2.0","generator":"DecentraLabs Smart Warehouse Cell"},
    "scene":0,"scenes":[scene],"nodes":node_list,"meshes":meshes,"materials":materials,
    "images":surface_images,
    "samplers":[{"magFilter":9729,"minFilter":9987,"wrapS":10497,"wrapT":10497}],
    "textures":[{"sampler":0,"source":index} for index in range(len(surface_images))],
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
