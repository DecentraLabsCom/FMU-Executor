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
                point = interpolate(corners[0], corners[1], corners[2], corners[3], u, v)
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


def make_shipping_label_texture(width: int = 256, height: int = 128) -> bytes:
    """Create a small printable barcode label for each textured carton."""
    pixels = bytearray(width * height * 3)

    def fill_rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(max(0, y0), min(height, y1)):
            for x in range(max(0, x0), min(width, x1)):
                offset = (y * width + x) * 3
                pixels[offset:offset + 3] = bytes(color)

    fill_rect(0, 0, width, height, (238, 233, 216))
    fill_rect(8, 8, 248, 120, (232, 227, 210))
    fill_rect(8, 8, 248, 24, (17, 57, 69))
    fill_rect(17, 34, 155, 39, (54, 82, 88))
    fill_rect(17, 44, 116, 48, (108, 126, 125))
    fill_rect(17, 53, 137, 57, (108, 126, 125))
    fill_rect(166, 34, 238, 66, (224, 232, 220))
    fill_rect(171, 39, 233, 43, (29, 102, 114))
    fill_rect(171, 48, 219, 52, (112, 137, 133))
    fill_rect(171, 57, 228, 61, (112, 137, 133))
    x = 17
    for index in range(62):
        bar_width = 1 + ((index * 7 + 3) % 4 == 0) + ((index * 13 + 1) % 11 == 0)
        if index % 9 in (0, 1):
            bar_width += 1
        fill_rect(x, 72, x + bar_width, 108, (17, 33, 38))
        x += bar_width + (1 if index % 5 else 2)
        if x >= 237:
            break
    fill_rect(17, 112, 126, 115, (209, 119, 46))

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        start = y * width * 3
        rows.extend(pixels[start:start + width * 3])
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _png_chunk(b"IEND", b"")


materials = [
    {"name":"Obsidian frame","pbrMetallicRoughness":{"baseColorFactor":[.055,.095,.13,1],"metallicFactor":.46,"roughnessFactor":.43}},
    {"name":"Safety amber","pbrMetallicRoughness":{"baseColorFactor":[1,.54,.08,1],"metallicFactor":.08,"roughnessFactor":.48}},
    {"name":"Brushed alloy","pbrMetallicRoughness":{"baseColorFactor":[.48,.60,.67,1],"metallicFactor":.34,"roughnessFactor":.52}},
    {"name":"Aisle teal","pbrMetallicRoughness":{"baseColorFactor":[.03,.49,.51,1],"metallicFactor":.06,"roughnessFactor":.55}},
    {"name":"Parcel coral","pbrMetallicRoughness":{"baseColorFactor":[.96,.31,.16,1],"metallicFactor":.05,"roughnessFactor":.55}},
    {"name":"Parcel blue","pbrMetallicRoughness":{"baseColorFactor":[.12,.38,.82,1],"metallicFactor":.12,"roughnessFactor":.42}},
    {"name":"Parcel gold","pbrMetallicRoughness":{"baseColorFactor":[1,.72,.13,1],"metallicFactor":.08,"roughnessFactor":.46}},
    {"name":"Warm timber","pbrMetallicRoughness":{"baseColorFactor":[.42,.27,.15,1],"metallicFactor":.01,"roughnessFactor":.82}},
    {"name":"Status lamp","emissiveFactor":[.015,.025,.028],"pbrMetallicRoughness":{"baseColorFactor":[.24,.35,.36,1],"metallicFactor":.06,"roughnessFactor":.32}},
    {"name":"Floor","pbrMetallicRoughness":{"baseColorFactor":[.27,.32,.32,1],"metallicFactor":.04,"roughnessFactor":.88}},
    {"name":"Glass cyan","emissiveFactor":[.005,.045,.052],"pbrMetallicRoughness":{"baseColorFactor":[.04,.42,.48,1],"metallicFactor":.12,"roughnessFactor":.4}},
    {"name":"Carbon black","pbrMetallicRoughness":{"baseColorFactor":[.018,.027,.034,1],"metallicFactor":.24,"roughnessFactor":.62}},
    {"name":"Courier blue","pbrMetallicRoughness":{"baseColorFactor":[.045,.20,.36,1],"metallicFactor":.12,"roughnessFactor":.5}},
    {"name":"Fleet white","pbrMetallicRoughness":{"baseColorFactor":[.78,.85,.84,1],"metallicFactor":.06,"roughnessFactor":.52}},
    {"name":"Safety glass","emissiveFactor":[.008,.025,.032],"pbrMetallicRoughness":{"baseColorFactor":[.055,.25,.29,1],"metallicFactor":.12,"roughnessFactor":.34}},
    {"name":"Concrete trim","pbrMetallicRoughness":{"baseColorFactor":[.13,.19,.22,1],"metallicFactor":.04,"roughnessFactor":.84}},
    {"name":"Signal cyan","emissiveFactor":[.01,.16,.13],"pbrMetallicRoughness":{"baseColorFactor":[.08,.80,.69,1],"metallicFactor":.08,"roughnessFactor":.34}},
    {"name":"Signal amber","emissiveFactor":[.16,.055,.006],"pbrMetallicRoughness":{"baseColorFactor":[1,.39,.035,1],"metallicFactor":.06,"roughnessFactor":.36}},
    {"name":"Road graphite","pbrMetallicRoughness":{"baseColorFactor":[.025,.04,.05,1],"metallicFactor":.015,"roughnessFactor":.96}},
    {"name":"Tire rubber","pbrMetallicRoughness":{"baseColorFactor":[.014,.018,.021,1],"metallicFactor":0,"roughnessFactor":.94}},
    {"name":"Warehouse cladding","pbrMetallicRoughness":{"baseColorFactor":[.11,.20,.24,1],"metallicFactor":.12,"roughnessFactor":.8}},
    {"name":"Warehouse foundation","pbrMetallicRoughness":{"baseColorFactor":[.10,.15,.17,1],"metallicFactor":.06,"roughnessFactor":.86}},
    {"name":"Rack teal steel","pbrMetallicRoughness":{"baseColorFactor":[.055,.25,.31,1],"metallicFactor":.25,"roughnessFactor":.54}},
    {"name":"Rack deck graphite","pbrMetallicRoughness":{"baseColorFactor":[.09,.14,.16,1],"metallicFactor":.34,"roughnessFactor":.64}},
    {"name":"Parcel tape","pbrMetallicRoughness":{"baseColorFactor":[.72,.64,.48,1],"metallicFactor":.01,"roughnessFactor":.82}},
    {"name":"Shipping label","pbrMetallicRoughness":{"baseColorFactor":[1,1,1,1],"metallicFactor":0,"roughnessFactor":.86}},
    {"name":"Clerestory glass","emissiveFactor":[.003,.015,.02],"alphaMode":"BLEND","doubleSided":True,"pbrMetallicRoughness":{"baseColorFactor":[.035,.18,.22,.64],"metallicFactor":.05,"roughnessFactor":.38}},
]
MAT_WALL = 20
MAT_WALL_BASE = 21
MAT_RACK = 22
MAT_RACK_DECK = 23
MAT_TAPE = 24
MAT_LABEL = 25
MAT_GLASS = 26
for index in (4, 5, 6, 7):
    materials[index]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
materials[2]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 1}
for index in (9, 15):
    materials[index]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 2}
materials[18]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 3}
for index in (MAT_WALL, MAT_RACK, MAT_RACK_DECK):
    materials[index]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 1}
materials[MAT_WALL_BASE]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 2}
materials[MAT_LABEL]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 4}
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


def add_carton_details(name: str, material: int, size, *, bind_size: bool = False):
    """Build taped, labeled package geometry in the package's local space."""
    width, height, depth = size
    label_bindings = [
        {"variable": "packageSizeX", "target": "position.x", "scale": .20},
        {"variable": "packageSizeY", "target": "position.y", "scale": -.12},
        {"variable": "packageSizeZ", "target": "position.z", "scale": .5, "offset": .018},
        {"variable": "packageSizeX", "target": "scale.x", "scale": .43},
        {"variable": "packageSizeY", "target": "scale.y", "scale": .25},
    ]
    tape_bindings = [
        {"variable": "packageSizeZ", "target": "position.z", "scale": .5, "offset": .010},
        {"variable": "packageSizeX", "target": "scale.x", "scale": .13},
        {"variable": "packageSizeY", "target": "scale.y", "scale": .96},
    ]
    top_tape_bindings = [
        {"variable":"packageSizeY","target":"position.y","scale":.5,"offset":.008},
        {"variable":"packageSizeX","target":"scale.x","scale":.13},
        {"variable":"packageSizeZ","target":"scale.z","scale":.96},
    ]
    body_bindings = [
        {"variable":"packageSizeX","target":"scale.x"},
        {"variable":"packageSizeY","target":"scale.y"},
        {"variable":"packageSizeZ","target":"scale.z"},
    ]
    def with_size_bindings(bindings):
        return {"fmiBindings":bindings} if bind_size else None

    return [
        add_node(
            f"{name}_Body", rounded_box_meshes[material], (0, 0, 0), size,
            extras=with_size_bindings(body_bindings),
        ),
        add_node(
            f"{name}_TapeFront", box_meshes[MAT_TAPE],
            (0, 0, depth * .5 + .010), (width * .13, height * .96, .016),
            extras=with_size_bindings(tape_bindings),
        ),
        add_node(
            f"{name}_TapeTop", box_meshes[MAT_TAPE],
            (0, height * .5 + .008, 0), (width * .13, .014, depth * .96),
            extras=with_size_bindings(top_tape_bindings),
        ),
        add_node(
            f"{name}_ShippingLabel", box_meshes[MAT_LABEL],
            (width * .20, -height * .12, depth * .5 + .018), (width * .43, height * .25, .014),
            extras=with_size_bindings(label_bindings),
        ),
    ]


def add_static_carton(name: str, material: int, position, size, *, extras=None):
    return add_group(name, position, children=add_carton_details(name, material, size), extras=extras)


def add_colored_package(name: str, visibility_variable: str, *, position=(0, 0, 0), agv=False, transfer=False):
    children = []
    for material_index, (color_name, color_variable) in zip(PACKAGE_MATERIALS, PACKAGE_COLOR_BINDINGS):
        variant_bindings = [{"variable":color_variable,"target":"visible"}]
        variant_children = add_carton_details(f"{name}{color_name}", material_index, PACKAGE_SIZES[0], bind_size=True)
        # Keep the source-package color selector on the variant parent so its
        # body, tape and label always appear and disappear together.
        children.append(add_group(
            f"{name}{color_name}", (0, 0, 0), children=variant_children,
            extras={"fmiBindings":variant_bindings},
        ))

    package_bindings: list[dict[str, str | float]] = [{"variable":visibility_variable,"target":"visible"}]
    if agv:
        package_bindings.append({"variable":"packageSizeY","target":"position.y","scale":.5,"offset":.585})
    if transfer:
        package_bindings.extend([
            {"variable":"transferPackageX","target":"position.x"},
            {"variable":"transferPackageY","target":"position.y"},
            {"variable":"transferPackageZ","target":"position.z"},
        ])
    return add_group(name, position, children=children, extras={"fmiBindings":package_bindings})


roots = []
roots.append(add_node("WarehouseFoundation", box_meshes[15], (0,-.31,1.8), (22.2,.45,21.0)))
roots.append(add_node("WarehouseFloor", box_meshes[9], (0,-.08,1.8), (21.8,.20,20.6)))
roots.append(add_node("LoadingApron", box_meshes[18], (0,-.09,9.1), (18.4,.22,4.4)))
roots.append(add_node("CentralAisle", box_meshes[3], (.7,.027,-.12), (11.4,.026,2.0)))
for z in (-1.16,.92):
    roots.append(add_node(f"AisleSafetyLine_{z}", box_meshes[1], (.7,.046,z), (11.0,.018,.052)))
for x in range(-8, 9):
    roots.append(add_node(f"FloorGuide_{x}", box_meshes[2], (x,.025,1.3), (.018,.008,15.8)))

# A broad dock apron, traffic markings, bumpers and painted pedestrian lanes.
for x in (-7.3, -5.4, 5.4, 7.3):
    roots.append(add_node(f"ApronLane_{x}", box_meshes[1], (x,.012,8.95), (.045,.018,3.8)))
for z in (7.1, 10.9):
    roots.append(add_node(f"ApronStopLine_{z}", box_meshes[2], (0,.015,z), (15,.025,.055)))
for x in (-7.4, -2.3, 2.3, 7.4):
    roots.append(add_node(f"DockBollard_{x}", box_meshes[1], (x,.52,5.72), (.20,1.04,.24)))

# Sheet-metal walls, translucent clerestory glazing and open loading docks enclose the cell.
warehouse_side_x = 10.55
warehouse_back_z = -8.42
dock_wall_z = 5.52
side_wall_length = dock_wall_z - warehouse_back_z
side_wall_center_z = (dock_wall_z + warehouse_back_z) / 2
side_wall_bay_length = side_wall_length / 4
for side in (-1, 1):
    wall_x = side * warehouse_side_x
    roots.append(add_node(f"WarehouseSideWallBase_{side}", box_meshes[MAT_WALL_BASE], (wall_x,.56,side_wall_center_z), (.42,1.12,side_wall_length)))
    for bay in range(4):
        z = warehouse_back_z + side_wall_bay_length * (bay + .5)
        roots.append(add_node(f"WarehouseSideWallPanel_{side}_{bay}", box_meshes[MAT_WALL], (wall_x,2.99,z), (.20,3.78,side_wall_bay_length)))
        roots.append(add_node(f"WarehouseSideClerestory_{side}_{bay}", box_meshes[MAT_GLASS], (wall_x,5.61,z), (.15,1.28,side_wall_bay_length)))
        for rib_index, rib_z in enumerate(range(round((z - side_wall_bay_length / 2 + .18) * 100), round((z + side_wall_bay_length / 2 - .18) * 100), 62)):
            roots.append(add_node(f"WarehouseSideWallRib_{side}_{bay}_{rib_index}", box_meshes[MAT_RACK_DECK], (wall_x,2.99,rib_z / 100), (.23,3.82,.055)))
    roots.append(add_node(f"WarehouseSideGuard_{side}", box_meshes[1], (wall_x,1.19,side_wall_center_z), (.27,.12,side_wall_length)))
    roots.append(add_node(f"WarehouseSideTopCap_{side}", box_meshes[MAT_RACK_DECK], (wall_x,6.38,side_wall_center_z), (.30,.20,side_wall_length)))

roots.append(add_node("WarehouseRearWallBase", box_meshes[MAT_WALL_BASE], (0,.56,warehouse_back_z), (21.10,1.12,.42)))
for bay, x in enumerate((-8.42,-4.21,0,4.21,8.42)):
    roots.append(add_node(f"WarehouseRearWallPanel_{bay}", box_meshes[MAT_WALL], (x,2.99,warehouse_back_z), (4.20,3.78,.20)))
    roots.append(add_node(f"WarehouseRearClerestory_{bay}", box_meshes[MAT_GLASS], (x,5.61,warehouse_back_z), (4.20,1.28,.15)))
    for rib_index in range(6):
        rib_x = x - 1.58 + rib_index * .62
        roots.append(add_node(f"WarehouseRearWallRib_{bay}_{rib_index}", box_meshes[MAT_RACK_DECK], (rib_x,2.99,warehouse_back_z), (.055,3.82,.23)))
roots.append(add_node("WarehouseRearTopCap", box_meshes[MAT_RACK_DECK], (0,6.38,warehouse_back_z), (21.10,.20,.30)))

for section, x, width in (("left",-8.60,3.80),("center",0,6.20),("right",8.60,3.80)):
    roots.append(add_node(f"WarehouseDockWall_{section}", box_meshes[MAT_WALL], (x,3.29,dock_wall_z), (width,6.58,.22)))
    for rib_index in range(max(1, int(width / .62))):
        rib_x = x - width / 2 + .28 + rib_index * .62
        roots.append(add_node(f"WarehouseDockWallRib_{section}_{rib_index}", box_meshes[MAT_RACK_DECK], (rib_x,3.29,dock_wall_z), (.055,6.50,.26)))

# Dock portals remain open at truck height and width; reinforced jambs and raised shutters frame them.
for bay, x in enumerate((-4.9,4.9), start=1):
    roots.append(add_node(f"WarehouseDockHeader_{bay}", box_meshes[MAT_WALL], (x,5.52,dock_wall_z), (3.60,2.12,.22)))
    roots.append(add_node(f"WarehouseDockRollerHousing_{bay}", box_meshes[11], (x,4.42,5.32), (3.96,.40,.40)))
    for side in (-1,1):
        roots.append(add_node(f"WarehouseDockJamb_{bay}_{side}", box_meshes[1], (x + side * 1.84,2.21,5.34), (.14,4.42,.30)))
    roots.append(add_node(f"WarehouseDockBumper_{bay}", box_meshes[12], (x,.30,5.34), (3.70,.22,.40)))
    roots.append(add_node(f"WarehouseDockSignal_{bay}", box_meshes[16], (x - 1.34,4.03,5.27), (2.80,.045,.025)))
    for slat in range(6):
        roots.append(add_node(f"WarehouseRaisedShutter_{bay}_{slat}", box_meshes[MAT_WALL_BASE], (x,4.62 + slat * .09,5.26), (3.12,.055,.08)))
    roots.append(add_node(f"WarehouseDockCanopy_{bay}", box_meshes[2], (x,5.0,6.1), (4.2,.16,1.15)))
    roots.append(add_node(f"WarehouseDockCanopyLight_{bay}", box_meshes[17], (x,4.87,6.08), (2.8,.035,.10)))
    for x_offset in (-1.35,-.90,-.45,0,.45,.90,1.35):
        roots.append(add_node(f"WarehouseDockStripe_{bay}_{x_offset}", box_meshes[1], (x + x_offset,.16,5.28), (.22,.12,.22)))

# The center wall hosts an illuminated status board between the dock openings.
roots.append(add_node("WarehouseWallStatusPanel", box_meshes[11], (0,5.37,5.43), (4.10,.84,.15)))
roots.append(add_node("WarehouseWallStatusScreen", box_meshes[3], (0,5.37,5.34), (3.78,.58,.025)))
for index in range(8):
    roots.append(add_node(f"WarehouseWallStatusSignal_{index}", box_meshes[16 if index % 2 == 0 else 17], (-1.47 + index * .42,5.37,5.31), (.22,.22,.025)))

# Roof trusses add scale above the sheet-metal shell without obscuring the work area.
for x in (-8.25, 8.25):
    for z in (-6.1, -2.1, 2.0, 5.5):
        roots.append(add_node(f"ShellColumn_{x}_{z}", box_meshes[0], (x,3.25,z), (.28,6.5,.28)))
    roots.append(add_node(f"ShellRunner_{x}", box_meshes[15], (x,6.4,-.2), (.42,.30,12.1)))
for z in (-6.1, -2.1, 2.0, 5.5):
    roots.append(add_node(f"RoofCrossbeam_{z}", box_meshes[15], (0,6.38,z), (16.7,.25,.28)))
for x in (-6.7, -2.3, 2.3, 6.7):
    roots.append(add_node(f"RoofBrace_{x}", box_meshes[0], (x,6.63,-.2), (.12,.12,12.4)))

# Ceiling luminaires use the standard glTF punctual-light extension so the
# generic Marketplace GLB viewer can reproduce the same warm/cool warehouse depth.
punctual_lights = []
for index, z in enumerate((-4.8,-1.2,2.4,5.2)):
    cool = index % 2 == 1
    light_color = [.62,.84,1.0] if cool else [1.0,.77,.52]
    light_index = len(punctual_lights)
    punctual_lights.append({"name":f"WarehouseHighBay_{index + 1}","type":"point","color":light_color,"intensity":12,"range":17})
    housing = add_node(f"RoofLightHousing_{index}", box_meshes[11], (0,6.0,z), (4.4,.16,.40))
    roots.append(housing)
    roots.append(add_node(f"RoofLightStrip_{index}", box_meshes[16 if cool else 17], (0,5.88,z), (3.9,.035,.10)))
    light_node = add_group(f"WarehouseHighBayLight_{index + 1}", (2.2 if cool else -2.2,5.65,z))
    node_list[light_node]["extensions"] = {"KHR_lights_punctual":{"light":light_index}}
    roots.append(light_node)

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
    children.append(add_node(f"{name}_front_grille", box_meshes[11], (0,1.18,2.86), (.84,.38,.075)))
    for grille_line in range(5):
        children.append(add_node(f"{name}_grille_rib_{grille_line}", box_meshes[2], (-.30 + grille_line * .15,1.18,2.91), (.035,.29,.025)))
    children.append(add_node(f"{name}_bumper", box_meshes[2], (0,.69,2.83), (2.30,.24,.22)))
    for side in (-1, 1):
        children.append(add_node(f"{name}_mirror_{side}", box_meshes[2], (side*1.18,1.56,2.46), (.13,.13,.36)))
        children.append(add_node(f"{name}_headlamp_{side}", box_meshes[16], (side*.76,1.0,2.84), (.30,.22,.06)))
        children.append(add_node(f"{name}_cab_side_window_{side}", box_meshes[14], (side*1.09,1.66,1.96), (.055,.54,.74)))
        children.append(add_node(f"{name}_cab_door_handle_{side}", box_meshes[2], (side*1.105,1.33,1.69), (.045,.055,.22)))
        children.append(add_node(f"{name}_cab_step_{side}", box_meshes[11], (side*.93,.77,1.72), (.34,.11,.48)))
        children.append(add_node(f"{name}_side_stripe_{side}", box_meshes[16], (side*1.38,1.45,-.70), (.055,.10,3.12)))
        for wheel_z in (-2.0, -1.35, .78, 2.03):
            wheel_rotation = (0,0,.70710678,.70710678)
            children.append(add_node(f"{name}_wheel_{side}_{wheel_z}", wheel_mesh, (side*1.34,.42,wheel_z), (.78,.29,.78), rotation=wheel_rotation))
            children.append(add_node(f"{name}_wheel_hub_{side}_{wheel_z}", carrier_wheel_hub_mesh, (side*1.49,.42,wheel_z), (.44,.065,.44), rotation=wheel_rotation))
    for side in (-1, 1):
        for rib_z in (-1.9, -1.45, -1.0, -.55, -.1, .35, .8):
            children.append(add_node(f"{name}_trailer_rib_{side}_{rib_z}", box_meshes[2], (side*1.37,2.0,rib_z), (.055,2.4,.045)))
    if open_loading_door:
        for index in range(6):
            slot_x = (0.0, -.70, .70)[index % 3]
            slot_z = (-.58, -1.58)[index // 3]
            package_size = PACKAGE_SIZES[index]
            children.append(add_static_carton(
                f"TruckCargo_{index + 1}", PACKAGE_MATERIALS[index % 3],
                (slot_x,.85 + package_size[1] * .5,slot_z), package_size,
                extras={"fmiBindings":[{
                    "variable":"truckLoadCount",
                    "target":"visible",
                    "visibleWhenGreaterThanOrEqual":index + 1,
                }]},
            ))
    roots.append(add_group(name, (x,0,z), children=children))

add_truck("InboundDeliveryTruck", -4.9, 7.85, 13)
add_truck("OutboundDeliveryTruck", 4.9, 7.85, 13, open_loading_door=True)

def add_pallet(name: str, x: float, y: float, z: float, width: float = .82, depth: float = .82):
    for index in (-1, 0, 1):
        roots.append(add_node(f"{name}_deck_{index}", box_meshes[7], (x,y,z + index * depth * .36), (width,.045,.13)))
    for index, offset in enumerate((-.34,0,.34)):
        roots.append(add_node(f"{name}_runner_{index}", box_meshes[7], (x + width * offset,y - .055,z), (.13,.10,depth * .92)))


# The high-bay picking rack follows the full length of the AGV's marked travel lane.
rack_front_z = -2.35
rack_back_z = -4.22
rack_min_x = -4.90
rack_max_x = 6.15
rack_center_x = (rack_min_x + rack_max_x) / 2
rack_length = rack_max_x - rack_min_x
rack_x_positions = [rack_min_x + index * rack_length / 7 for index in range(8)]
for x in rack_x_positions:
    for z in (rack_front_z, rack_back_z):
        roots.append(add_node(f"LongRack_upright_{x}_{z}", box_meshes[MAT_RACK], (x,2.78,z), (.15,5.52,.16)))
        roots.append(add_node(f"LongRack_base_{x}_{z}", box_meshes[11], (x,.07,z), (.31,.07,.30)))
        for slot_index, y in enumerate([.45 + slot * .28 for slot in range(18)]):
            roots.append(add_node(f"LongRack_upright_slot_{x}_{z}_{slot_index}", box_meshes[11], (x,y,z + (.083 if z == rack_front_z else -.083)), (.17,.035,.018)))

for shelf_y in (.48,1.94,3.72,5.20):
    for z in (rack_front_z,rack_back_z):
        roots.append(add_node(f"LongRack_beam_{shelf_y}_{z}", box_meshes[1], (rack_center_x,shelf_y,z), (rack_length,.14,.11)))
    roots.append(add_node(f"LongRack_deck_{shelf_y}", box_meshes[MAT_RACK_DECK], (rack_center_x,shelf_y - .11,(rack_front_z + rack_back_z) / 2), (rack_length,.09,1.68)))
    for slat_index, x in enumerate([rack_min_x + .06 + slat * .38 for slat in range(29)]):
        roots.append(add_node(f"LongRack_deck_slat_{shelf_y}_{slat_index}", box_meshes[2], (x,shelf_y - .045,(rack_front_z + rack_back_z) / 2), (.035,.04,1.52)))

for bay_index, (start_x, end_x) in enumerate(zip(rack_x_positions, rack_x_positions[1:])):
    dx = end_x - start_x
    dy = 4.55
    brace_length = math.hypot(dx, dy)
    brace_angle = math.atan2(dy, dx)
    center_x = (start_x + end_x) / 2
    for side, z in (("front",rack_front_z - .095),("back",rack_back_z + .095)):
        roots.append(add_node(f"LongRack_brace_{bay_index}_{side}_up", box_meshes[2], (center_x,2.70,z), (brace_length,.05,.07), rotation=(0,0,math.sin(brace_angle / 2),math.cos(brace_angle / 2))))
        roots.append(add_node(f"LongRack_brace_{bay_index}_{side}_down", box_meshes[2], (center_x,2.70,z), (brace_length,.05,.07), rotation=(0,0,-math.sin(brace_angle / 2),math.cos(brace_angle / 2))))

# The shelf levels carry reserve cartons across the extended bay, leaving the
# model-driven pickup tier clear except for the six individually tracked boxes.
for tier_index, (shelf_y,z,step,size) in enumerate(((.48,-3.08,1.18,(.62,.55,.54)),(3.72,-3.54,.92,(.62,.55,.54)),(5.20,-3.08,1.18,(.72,.46,.62)))):
    x = rack_min_x + .35
    carton_index = 0
    while x <= rack_max_x - .35:
        if not (tier_index == 0 and x < -3.9):
            deck_top = shelf_y - .065
            roots.append(add_static_carton(
                f"LongRackStock_{tier_index}_{carton_index}",
                PACKAGE_MATERIALS[(carton_index + tier_index + 1) % 3],
                (x,deck_top + size[1] / 2,z), size,
            ))
        x += step
        carton_index += 1

# Six individually tracked parcels occupy two shelf pallets. Each box is small
# enough for the AGV deck, the loading belt and the truck cargo slots.
for index, (package_x, package_size) in enumerate(zip(SOURCE_PACKAGE_X, PACKAGE_SIZES), start=1):
    pallet_y = 1.965
    add_pallet(f"ShelfPackage_{index}_pallet", package_x, pallet_y, -3.08, max(package_size[0] + .20,.72), max(package_size[2] + .16,.78))
    roots.append(add_static_carton(
        f"ShelfPackage_{index}", PACKAGE_MATERIALS[(index - 1) % 3],
        (package_x,2.01 + package_size[1] * .5,-3.08), package_size,
        extras={"fmiBindings":[{"variable":f"shelfPackage{index}Visible","target":"visible"}]},
    ))

# A second double-sided rack occupies the central bay between both loading docks.
middle_rack_min_x = -3.25
middle_rack_max_x = 3.25
middle_rack_length = middle_rack_max_x - middle_rack_min_x
middle_rack_center_x = (middle_rack_min_x + middle_rack_max_x) / 2
middle_rack_front_z = 2.50
middle_rack_back_z = 4.08
middle_rack_center_z = (middle_rack_front_z + middle_rack_back_z) / 2
middle_rack_posts = [middle_rack_min_x + index * middle_rack_length / 4 for index in range(5)]
for x in middle_rack_posts:
    for z in (middle_rack_front_z,middle_rack_back_z):
        roots.append(add_node(f"CenterRack_upright_{x}_{z}", box_meshes[MAT_RACK], (x,2.31,z), (.14,4.62,.14)))
        roots.append(add_node(f"CenterRack_base_{x}_{z}", box_meshes[11], (x,.07,z), (.28,.07,.28)))
for shelf_y in (.55,1.80,3.05,4.30):
    for z in (middle_rack_front_z,middle_rack_back_z):
        roots.append(add_node(f"CenterRack_beam_{shelf_y}_{z}", box_meshes[1], (middle_rack_center_x,shelf_y,z), (middle_rack_length,.13,.11)))
    roots.append(add_node(f"CenterRack_deck_{shelf_y}", box_meshes[MAT_RACK_DECK], (middle_rack_center_x,shelf_y - .11,middle_rack_center_z), (middle_rack_length,.09,1.78)))
    for slat_index, x in enumerate([middle_rack_min_x + .10 + slat * .32 for slat in range(20)]):
        roots.append(add_node(f"CenterRack_deck_slat_{shelf_y}_{slat_index}", box_meshes[2], (x,shelf_y - .045,middle_rack_center_z), (.035,.04,1.64)))
for bay_index, (start_x,end_x) in enumerate(zip(middle_rack_posts,middle_rack_posts[1:])):
    brace_dy = 1.24
    brace_dx = end_x - start_x
    brace_length = math.hypot(brace_dx,brace_dy)
    brace_angle = math.atan2(brace_dy,brace_dx)
    for z in (middle_rack_front_z - .08,middle_rack_back_z + .08):
        roots.append(add_node(f"CenterRack_brace_{bay_index}_up_{z}", box_meshes[2], ((start_x + end_x) / 2,1.04,z), (brace_length,.05,.065), rotation=(0,0,math.sin(brace_angle / 2),math.cos(brace_angle / 2))))
        roots.append(add_node(f"CenterRack_brace_{bay_index}_down_{z}", box_meshes[2], ((start_x + end_x) / 2,1.04,z), (brace_length,.05,.065), rotation=(0,0,-math.sin(brace_angle / 2),math.cos(brace_angle / 2))))

middle_rack_carton_x = [(left + right) / 2 for left,right in zip(middle_rack_posts,middle_rack_posts[1:])]
middle_rack_stock_index = 0
for shelf_y in (.55,1.80,3.05):
    for z in (2.88,3.70):
        for x in middle_rack_carton_x:
            size = PACKAGE_SIZES[middle_rack_stock_index % len(PACKAGE_SIZES)]
            material_index = PACKAGE_MATERIALS[middle_rack_stock_index % len(PACKAGE_MATERIALS)]
            deck_top = shelf_y - .065
            add_pallet(f"CenterRack_pallet_{middle_rack_stock_index}", x, deck_top, z, max(size[0] + .18,.76), max(size[2] + .16,.72))
            roots.append(add_static_carton(
                f"CenterRack_stock_{middle_rack_stock_index}", material_index,
                (x,deck_top + .05 + size[1] / 2,z), size,
            ))
            middle_rack_stock_index += 1

# The warehouse collection belt, rollers and sort gate at the rear.
roots.append(add_node("InboundConveyorFrame", box_meshes[0], (0,.55,-4.62), (7.4,.44,.92)))
roots.append(add_node("InboundConveyorBelt", box_meshes[3], (0,.80,-4.62), (7.15,.08,.78)))
for x in (-3.2,-2.4,-1.6,-.8,0,.8,1.6,2.4,3.2):
    roots.append(add_node(f"ConveyorRoller_{x}", inbound_roller_mesh, (x,.55,-4.62), (.20,.98,.20), rotation=(0,0,.70710678,.70710678)))
roots.append(add_node("SortGate", box_meshes[1], (2.6,1.05,-4.62), (.18,.48,.95)))
roots.append(add_static_carton("InboundParcel", 4, (-2.6,1.06,-4.62), (.72,.50,.62)))

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
picker_rail_min_x = rack_min_x + .35
picker_rail_max_x = rack_max_x - .35
picker_rail_center_x = (picker_rail_min_x + picker_rail_max_x) / 2
picker_rail_length = picker_rail_max_x - picker_rail_min_x
for side, x in (("Left",picker_rail_min_x),("Right",picker_rail_max_x)):
    roots.append(add_node(f"PickArmRailColumn{side}", box_meshes[2], (x,1.63,-3.08), (.22,3.26,.22)))
roots.append(add_node("PickArmRail", box_meshes[1], (picker_rail_center_x,3.43,-3.08), (picker_rail_length,.16,.34)))
roots.append(add_node("PickArmRailGuide", box_meshes[2], (picker_rail_center_x,3.32,-3.08), (picker_rail_length - .26,.07,.20)))
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
surface_images.append({
    "name":"FMI parcel shipping label",
    "bufferView":add_buffer(make_shipping_label_texture()),
    "mimeType":"image/png",
})
gltf = {
    "asset":{"version":"2.0","generator":"DecentraLabs Smart Warehouse Cell"},
    "scene":0,"scenes":[scene],"nodes":node_list,"meshes":meshes,"materials":materials,
    "images":surface_images,
    "samplers":[{"magFilter":9729,"minFilter":9987,"wrapS":10497,"wrapT":10497}],
    "textures":[{"sampler":0,"source":index} for index in range(len(surface_images))],
    "buffers":[{"byteLength":len(binary)}],"bufferViews":buffer_views,"accessors":accessors,
    "extensionsUsed":["KHR_lights_punctual"],
    "extensions":{"KHR_lights_punctual":{"lights":punctual_lights}},
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
