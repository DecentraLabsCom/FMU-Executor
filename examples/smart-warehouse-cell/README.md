# Smart Warehouse Cell

An FMI 2.0 Co-Simulation example for an automated warehouse. The deterministic
FMU models a complete order fulfillment cycle: a ceiling-suspended trolley runs
behind the full-length rack to one of six individually sized cartons, lowers
and closes its gripper, lifts the carton clear of the rack, and returns to the
AGV handoff point. The
low-profile AGV carries each carton on a guarded load deck toward a delivery
truck, where a conveyor and loader arm place it inside. The cart then returns
for another parcel.
The cycle ends when the requested quantity is loaded or the truck reaches its
six-carton capacity.

The example ships with these companion artifacts:

- `SmartWarehouseCell.fmu`: Windows x64 build for Lab Station on Windows.
- `SmartWarehouseCell-linux64.fmu`: Linux x86_64 build for Gateway development
  and Lab Station Linux.
- `smart-warehouse-cell.glb`: the default GLB exported from the same procedural
  Three.js scene as the web app. It includes `extras.fmiBindings` for rack
  pickup, AGV travel, conveyor transfer, loader arm, truck cargo slots, lift and
  alarm lamp, and is embedded in the distributed FMUs.
- `SmartWarehouseCell.webapp.zip`: optional static Three.js application with
  its own procedural warehouse scene. It bundles its viewer modules and does
  not include or load the companion GLB. Marketplace grants it a reservation-
  scoped Gateway session, and the app receives FMI samples directly over WS.
  It needs no separate asset server or CDN.

The scene includes high-bay racks, roof structure, loading docks, a centered
animated roller conveyor and an outbound truck with an open loading door. The
four-wheel carrier cart has a clear blue chassis, visible wheels, a liftable
flat load deck, amber cargo guides and forward lights. Six
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
The picker rail is suspended from the warehouse roof behind the full-width
storage rack. The trolley first travels with its boom retracted, lowers the
vertical mast behind the rack, extends into the selected shelf bay at package
height, and lowers the gripper to take the carton. Source cartons sit between
the rack uprights so the boom has a clear approach. The parcel then follows the
same modeled path to the center of the AGV load deck before the trolley
retracts. The six animated parcels are spread across the rack so the trolley
traverses most of the storage bay.
The AGV stops with the front edge of its load tray aligned to the truck
conveyor's entry edge. Its powered tray rollers then move each parcel across
the tray and onto the transverse conveyor, whose rollers take over the truck
loading sequence. The conveyor rails stop short at the handoff so parcels enter
without catching the rail ends. The near-dock rack is narrowed to keep both
truck entrances clear. The FMU drives the overhead trolley, gripper motion,
package paths, AGV, both roller conveyors, loading arm, inventory, order status
and safety lamp.

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

The FMU declares its inputs and outputs in `modelDescription.xml`; GLB visual
bindings live in the scene nodes. The Three.js app implements Marketplace host
integration version 2 to announce readiness and request a one-use connection
grant for the active reservation. It opens the provider Gateway's FMU session
WebSocket, initializes the model, and reads bounded `sim.advance` batches
directly from the session. The app owns its FMU controls and presentation, so
it can build its HMI around the warehouse scene without generated simulation
controls being placed over it.

The app's duration control supplies `options.stopTime` (5-300 s). Its input
controls send declared FMU values during initialization. Marketplace can add
the optional input schedule editor and the Speed menu; the latter advertises
`playback-rate` and uses `playbackControlPosition: "bottom-right"`. Playback
rate changes visual replay speed only. The app reports execution state so the
host can offer cancellation and Fit to run duration. It receives no FMU binary,
access key, Gateway credential or Station secret. On startup, the app announces
itself to Marketplace and receives host readiness before reporting its scene
ready. The SDK client and low-level FMU WebSocket client are optional; this
bundle includes both to simplify the connection and playback integration.

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

The builder refreshes every JavaScript module in `webapp/sdk/` from the canonical
Marketplace package, including the adaptive batch controller and its input
quota helper, before packaging. It removes stale JavaScript modules and checks
that the app's local module and asset references are present in the generated
ZIP. Rebuild the webapp bundle after updating the Marketplace SDK.
The browser contract also accepts apps that implement its messages directly.

Both FMU builds embed the default Three.js-exported GLB in `resources/` and ship
a same-stem companion `.glb` for Gateway/Station asset import. The optional
`.webapp.zip` is also kept beside the FMU so Lab Manager can import it from
either FMU backend. Build scripts package the checked-in default scene as-is.

## Local scene authoring

To export the current Three.js app scene to the checked-in GLB, start the local
export helper from this directory:

```powershell
python tools/serve-scene-export.py --three-root ../../../Marketplace/node_modules/three
```

Open `http://127.0.0.1:8765/tools/export-scene.html` to generate
`smart-warehouse-cell.glb` from the app scene. The helper binds only to
loopback; it does not upload the scene or contact an external asset server.

## Demo profile

1. Provision `SmartWarehouseCell.fmu` as an FMU laboratory.
2. In Lab Manager, attach either the companion GLB or the Three.js app ZIP.
3. Sync the AAS and book/open the lab in Marketplace.
4. Set `orderQuantity` from one to six packages. `robotSpeed` controls AGV
   travel; `rushOrder` speeds up the next outbound trip by 45%; `jam` freezes
   the entire process and raises the safety alarm.
5. Choose a simulation duration from 5 to 300 seconds in the Three.js app and
   run the FMU. The app sends that duration through the standard
   `options.stopTime` field; shorter runs stop part-way through the cycle. The
   default 250-second run completes the six-package order at the default speed
   (the model finishes loading at about 245 seconds). The GLB option
   presents the authored asset through Marketplace's generic viewer; the
   Three.js option presents its separate native scene from the same FMI stream.

## License

The model, Three.js scene and app code are provided under the MIT license in
`LICENSE.txt`. The app bundle also includes the upstream Three.js license.

The app uses host integration v2 to obtain a one-use connection grant, then
executes directly over the provider Gateway WebSocket with the same session
authorization as proxy.fmu. Marketplace does not relay model samples. The app
bundle can also run independently when its SDK client is configured with a
`getConnection` callback supplied by the hosting service.
