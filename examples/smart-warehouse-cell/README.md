# Smart Warehouse Cell

An FMI 2.0 Co-Simulation example for an automated warehouse. The deterministic
FMU moves an AGV through a pick cycle, animates a robot arm and lift, tracks
orders and inventory, and raises an alarm when a jam is injected.

The example ships with three companion artifacts:

- `SmartWarehouseCell.fmu`: Windows x64 build for Lab Station on Windows.
- `SmartWarehouseCell-linux64.fmu`: Linux x86_64 build for Gateway development
  and Lab Station Linux.
- `smart-warehouse-cell.glb`: self-contained geometry with `extras.fmiBindings`
  for the AGV, parcel, robot arm, lift and alarm lamp.
- `SmartWarehouseCell.webapp.zip`: optional static Three.js application for a
  custom warehouse presentation. It contains the GLB, viewer modules and no
  external runtime dependency.

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
python build_webapp.py --three-root ../../Marketplace/node_modules/three
```

Both FMU builds embed the same generated GLB in `resources/` and ship a
same-stem companion `.glb` for Gateway/Station asset import. The optional
`.webapp.zip` is also kept beside the FMU so Lab Manager can import it from
either FMU backend.

## Demo profile

1. Provision `SmartWarehouseCell.fmu` as an FMU laboratory.
2. In Lab Manager, attach either the companion GLB or the Three.js app ZIP.
3. Sync the AAS and book/open the lab in Marketplace.
4. Set `conveyorSpeed` and optionally `rushOrder`; set `jam` to demonstrate
   the safety light and pause response.
5. Run the simulation. The default GLB viewer applies node bindings, or the
   app bundle presents the same live output stream in its own Three.js layout.

## License

The model, generated scene and app code are provided under the MIT license in
`LICENSE.txt`. The app bundle also includes the upstream Three.js license.
