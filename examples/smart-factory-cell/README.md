# Smart Factory Cell

A deterministic FMI 2.0 Co-Simulation example for a compact conveyor and robot
sorting cell. The C FMU drives the product position, robot shoulder, stack-light
alarm and part counters. The adjacent GLB is a self-contained Three.js scene;
its `extras.fmiBindings` entries point to FMI output names and contain only the
visual transform needed to connect each output to a GLB node.

## Build

To build the Windows x64 artifact, install Python 3.11+ and Visual Studio C++
Build Tools with the x64 compiler, then run from this directory:

```powershell
./build.ps1
```

This creates `SmartFactoryCell.fmu` with the Windows x64 FMI binary linked to
the static MSVC runtime, the FMI
model description and a copy of the GLB in `resources/`. The standalone
`smart-factory-cell.glb` is also generated next to the source. The prepared
scene already includes the geometry and `extras.fmiBindings` for the demo.

For Gateway development on Linux x64, install GCC and Python 3, then run
`bash ./build-linux.sh`. This creates `SmartFactoryCell-linux64.fmu` with the same
FMI interface and scene resource but a `binaries/linux64` implementation. Use
that Linux artifact as `fmu-data/SmartFactoryCell.fmu` in the Gateway local
executor profile by running Gateway's `scripts/sync_fmu_examples.py` with the
parent `examples` directory as `--examples-root`. Lab Station's release
packager copies the Windows x64 artifact directly from this canonical example.
The Linux Lab Station amd64 release packages the Linux x64 artifact and its
same-stem GLB; the shared FMU Executor serves that scene to Gateway through the
authenticated companion-asset endpoint. The Linux arm64 release omits this
x86_64-only example.

The Gateway development package keeps the FMU and standalone GLB together in
`fmu-data/`. In production, Lab Station packages its platform FMU and the same
GLB side by side. When the provider synchronizes the AAS, Lab Manager imports
the companion GLB from the active FMU backend into the provider Gateway's
immutable public scene store. Station serves it only through its authenticated
internal endpoint; Marketplace receives only the Gateway asset path.

For another provider FMU, either host its self-contained GLB at a public HTTPS
URL or upload it in the provider Gateway's Lab Manager `Digital Twin
Management` panel. The Gateway upload keeps the scene in the provider's own
public, read-only asset store and returns its URL for the AAS sync; it does not
write to Marketplace. Marketplace resolves Gateway-managed scenes against the
lab's on-chain Gateway and loads them through a same-origin API, so the managed
scene does not require per-Gateway Marketplace CSP or browser CORS changes.
Public external HTTPS GLB URLs still require their origin in
`CSP_CONNECT_SRC` and browser CORS permission. Each GLB node that should
respond to an FMU output must carry an `extras.fmiBindings` entry; the viewer
does not infer scene geometry or signal-to-node mappings from the FMU.
Use a public asset URL without credentials or access tokens.

The binding lives in the glTF node's `extras` object and refers to an output
variable by its exact FMI name. `target` is one of `position.x/y/z`,
`rotation.x/y/z`, `scale.x/y/z`, `visible` or `material.color`. Numeric transform
bindings use `value * scale + offset` (`scale` defaults to `1`, `offset` to
`0`); `visible` and `material.color` are for Boolean outputs. For example:

```json
{
  "extras": {
    "fmiBindings": [
      { "variable": "partPosition", "target": "position.x", "scale": 1, "offset": -1.5 },
      { "variable": "alarm", "target": "material.color", "trueColor": "#ff2738", "falseColor": "#4f2028" }
    ]
  }
}
```

The source and generated scene use the MIT license in `LICENSE.txt`. The FMU
has no external runtime or content dependencies.

`demo-profiles.json` contains normal-production, single-reject and conveyor-jam
input profiles with expected final counters and alarm state for repeatable
integration checks.

## FMI interface

| Variable | Causality | Type | Start | Meaning |
| --- | --- | --- | --- | --- |
| `conveyorSpeed` | input | Real, m/s | 0.45 | Belt speed, range 0–1.2 m/s |
| `startStop` | input | Boolean | true | Run or stop the line |
| `jam` | input | Boolean | false | Stop the belt and light the alarm |
| `rejectNext` | input | Boolean | false | Reject the next completed part |
| `productPosition` | output | Real, m | 0 | Part position on a 3 m loop |
| `robotAngle` | output | Real, rad | 0 | Pick-and-place shoulder angle |
| `productPresent` | output | Boolean | true | Tracked product is present |
| `alarm` | output | Boolean | false | Jam/alarm state |
| `goodParts` | output | Integer | 0 | Accepted parts |
| `rejectedParts` | output | Integer | 0 | Rejected parts |
| `sortOffset` | output | Real, rad | 0 | Sorter-gate angle; negative sends the part to reject |

## AAS and Marketplace

FMI remains the source of variable names, types, causality, units and ranges.
To publish the scene location, synchronize generated FMU metadata with the
existing IDTA Handover Documentation submodel and set its optional GLB File
reference. Lab Manager imports the same-stem scene from the active FMU backend
automatically for this demo. For another model, its **Import companion GLB from
FMU backend** action copies the scene into the provider Gateway's public asset
store. The returned root-relative path is then saved in AAS as
`digitalTwinAssetUrl`. Direct API clients can use the same sequence:

```text
POST /lab-admin/digital-twin-assets/<labId>/from-fmu
Content-Type: application/json
{"accessKey":"SmartFactoryCell.fmu","assetType":"glb"}

POST /fmu/aas-admin/fmu/SmartFactoryCell.fmu/sync?labId=<labId>&digitalTwinAssetUrl=<url-encoded-path-from-import>
```

Marketplace resolves the root-relative AAS File path against the lab's Gateway
origin and reads bindings from the GLB node extras. No separate digital-twin
manifest is required for this example.

Set `jam=true` to stop the belt and turn on the red stack-light indicator. Set
`rejectNext=true` to increment the reject count when the current part reaches
the end of the conveyor. The simulation can be repeated with the same inputs
and produces the same trace.
