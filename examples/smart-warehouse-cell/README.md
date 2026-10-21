# Smart Warehouse Cell

An FMI 2.0 Co-Simulation example for an automated warehouse. The deterministic
FMU models a complete order fulfillment cycle: an overhead trolley slides along
a rail to one of six individually sized cartons, lowers and closes its gripper,
lifts the carton clear of the rack, and returns to the AGV handoff point. The
low-profile AGV carries each carton on a guarded roller deck toward a delivery
truck, where a conveyor and loader arm place it inside. The cart then returns
for another parcel.
The cycle ends when the requested quantity is loaded or the truck reaches its
six-carton capacity.

The example ships with these companion artifacts:

- `SmartWarehouseCell.fmu`: Windows x64 build for Lab Station on Windows.
- `SmartWarehouseCell-linux64.fmu`: Linux x86_64 build for Gateway development
  and Lab Station Linux.
- `smart-warehouse-cell.glb`: self-contained geometry with `extras.fmiBindings`
  for rack pickup, AGV travel, conveyor transfer, loader arm, truck cargo slots,
  lift and alarm lamp. It includes a closed warehouse shell with high windows,
  two open loading bays, a long high-bay rack, the center pallet rack, detailed
  cartons and dock lighting.
- `SmartWarehouseCell.webapp.zip`: optional static Three.js application with
  its own procedural warehouse scene. It bundles its viewer modules and does
  not include or load the companion GLB. Marketplace sends it the same live FMI
  samples as the GLB viewer; it needs no separate asset server or CDN.

The scene includes high-bay racks, roof structure, loading docks, a centered
animated roller conveyor and an outbound truck with an open loading door. The
four-wheel carrier cart has a clear blue chassis, visible wheels, a liftable
roller deck, amber cargo guides and forward lights. Six
different parcel sizes and colors are tracked separately: the selected shelf
package disappears as the gripper closes around it, and its dimensions and
color follow it onto the AGV, along the conveyor and into its truck slot. Cargo
slots fill from the truck's deep row toward the rear opening, leaving the access
path clear for later parcels.
The GLB embeds procedural PBR textures for cardboard, shipping labels, timber,
painted steel, concrete and dock asphalt, plus beveled cartons, a detailed
truck cab and the carrier cart. Its ribbed walls, high windows, rack bracing,
lit dock details and warm/cool punctual lights bring it closer to the richer
Three.js scene while preserving the GLB bindings in node metadata. Marketplace's
generic viewer adds its environment reflections and renders the GLB materials
and punctual lights.
The conveyor rails stop short at the AGV handoff so parcels enter without
catching the rail ends. The near-dock rack is narrowed to keep both truck
entrances clear. The FMU drives the overhead trolley, gripper motion, package
paths, AGV, loading arm, belt rollers, inventory, order status and safety lamp.

The Three.js app builds a separate, code-generated fulfillment hall: a long
multi-tier rack running beside the full AGV lane, a second loaded pallet rack
between the truck docks, corrugated warehouse walls with high windows and two
open loading bays, overhead picker rails, a moving gripper, a four-wheel
powered carrier, animated rollers, two detailed trucks, roof trusses, dock
signage and traffic markings. Textures, carton labels, rounded geometry,
warm/cool industrial lights and emissive safety strips are created locally by
the app. Its orbit, zoom, top-view, lighting and play/pause controls belong to
that full presentation. Both visualizations receive the same FMU outputs, but
GLB bindings are declared in the model asset while the Three.js app maps those
outputs directly to its own scene.

The FMU describes its own inputs and outputs in `modelDescription.xml`. The
visual bindings live with the scene nodes in the GLB. A static Three.js app
receives initial FMI values from Marketplace through the public
`decentralabs:fmi-sample` version 1 `postMessage` event, then receives newly
streamed timeline samples through `decentralabs:fmi-samples`. The host also
sends `decentralabs:fmi-reset` when a new run replaces the timeline and
`decentralabs:fmi-simulation-state` to indicate whether samples are still
arriving. The app never receives an FMU, access key or reservation credential.
It reports `decentralabs:digital-twin-ready` version 1 after its procedural
scene has initialized and reports `decentralabs:digital-twin-error` version 1
if scene construction fails.
Marketplace sends `decentralabs:digital-twin-host-ready` version 1 after the
iframe document loads; the app retains its startup result until that handshake
arrives.

## Build

Build the Windows FMU with Visual Studio C++ Build Tools:

```powershell
./build.ps1
```

Build the Linux x86_64 FMU with GCC:

```bash
./build-linux.sh
```

Build the browser app bundle from the installed Marketplace Three.js package:

```bash
python build_webapp.py --three-root ../../../Marketplace/node_modules/three
```

Both FMU builds embed the same generated GLB in `resources/` and ship a
same-stem companion `.glb` for Gateway/Station asset import. The optional
`.webapp.zip` is also kept beside the FMU so Lab Manager can import it from
either FMU backend.

## Demo profile

1. Provision `SmartWarehouseCell.fmu` as an FMU laboratory.
2. In Lab Manager, attach either the companion GLB or the Three.js app ZIP.
3. Sync the AAS and book/open the lab in Marketplace.
4. Set `orderQuantity` from one to six packages. `robotSpeed` controls AGV
   travel; `rushOrder` speeds up the next outbound trip by 45%; `jam` freezes
   the entire process and raises the safety alarm.
5. Run for about 200 seconds to see the full default six-package order at the
   default speed. Shorter runs stop part-way through the cycle. The GLB option
   presents the authored asset through Marketplace's generic viewer; the
   Three.js option presents its separate native scene from the same FMI stream.

## License

The model, generated scene and app code are provided under the MIT license in
`LICENSE.txt`. The app bundle also includes the upstream Three.js license.
