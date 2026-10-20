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
  lift and alarm lamp.
- `SmartWarehouseCell.webapp.zip`: optional static Three.js application for a
  custom warehouse presentation. It contains the GLB, viewer modules and no
  external runtime dependency.

The scene includes high-bay racks, roof structure, loading docks, a centered
animated roller conveyor and an outbound truck with an open loading door. The
four-wheel carrier cart has a clear blue chassis, visible wheels, a liftable
roller deck, amber cargo guides and forward lights. Six
different parcel sizes and colors are tracked separately: the selected shelf
package disappears as the gripper closes around it, and its dimensions and
color follow it onto the AGV, along the conveyor and into its truck slot. Cargo
slots fill from the truck's deep row toward the rear opening, leaving the access
path clear for later parcels.
The GLB embeds subtle procedural PBR textures for cardboard, brushed metal,
concrete and dock asphalt, plus softly beveled cartons, truck cab and cart body.
Tires use matte rubber while hubs and conveyor rollers remain metallic, and all
cylindrical parts use smoother geometry. Both Marketplace's
standard GLB viewer and the companion app add a studio reflection environment;
the app also lights the cell from warm and cool warehouse roof fixtures.
The conveyor rails stop short at the AGV handoff so parcels enter without
catching the rail ends. The near-dock rack is narrowed to keep both truck
entrances clear. The FMU drives the overhead trolley, gripper motion, package
paths, AGV, loading arm, belt rollers, inventory, order status and safety lamp.
The Three.js app offers orbit, zoom, top-view and lighting controls while
showing the same order progress and live FMI values.

The FMU describes its own inputs and outputs in `modelDescription.xml`. The
visual bindings live with the scene nodes in the GLB. A static Three.js app
receives the current FMI outputs from Marketplace through the public
`decentralabs:fmi-sample` version 1 `postMessage` event; it never receives an
FMU, access key or reservation credential.

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
4. Set `orderQuantity` from one to six packages. `conveyorSpeed` controls AGV
   travel; `rushOrder` speeds up the next outbound trip by 45%; `jam` freezes
   the entire process and raises the safety alarm.
5. Run for about 200 seconds to see the full default six-package order at the
   default speed. Shorter runs stop part-way through the cycle. The default GLB
   viewer applies node bindings, or the app bundle presents the same live output
   stream in its own Three.js layout.

## License

The model, generated scene and app code are provided under the MIT license in
`LICENSE.txt`. The app bundle also includes the upstream Three.js license.
