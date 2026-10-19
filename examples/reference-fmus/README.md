# FMI reference and test FMUs

This directory is the canonical source for the FMI example FMUs bundled with
Lab Gateway. The six reference models are unmodified FMU artifacts from
[Modelica Reference FMUs v0.0.39](https://github.com/modelica/Reference-FMUs/tree/v0.0.39):

| FMU | FMI | Linux x86_64 archive platform |
| --- | --- | --- |
| `BouncingBall.fmu` | 2.0 | `linux64` |
| `Dahlquist.fmu` | 2.0 | `linux64` |
| `Feedthrough.fmu` | 2.0 | `linux64` |
| `Stair.fmu` | 3.0 | `x86_64-linux` |
| `StateSpace.fmu` | 3.0 | `x86_64-linux` |
| `VanDerPol.fmu` | 3.0 | `x86_64-linux` |
| `BinaryClockTest.fmu` | 3.0 | `x86_64-linux` and `x86_64-windows` |

The FMI 2 and FMI 3 specifications use different directory names for their
Linux x86_64 binaries. These are platform entries inside the same FMU archives,
not separate duplicate FMU files.

`BinaryClockTest` is a DecentraLabs FMI 3 test model. Its C source and model
description are in `BinaryClockTest/`. Build either platform from that
directory; the packager preserves the other platform's binary in the existing
FMU:

```powershell
./build.ps1
```

```bash
bash ./build-linux.sh
```

From PowerShell on the workspace root, build the Linux binary with Docker:

```powershell
$sourceRoot = (Resolve-Path 'FMU-Executor/examples/reference-fmus').Path.Replace('\','/')
docker run --rm --mount "type=bind,source=$sourceRoot,target=/workspace" `
  --workdir /workspace/BinaryClockTest python:3.13-slim-bookworm sh -lc `
  "apt-get update && apt-get install -y --no-install-recommends build-essential && bash ./build-linux.sh"
```

To refresh Gateway's development examples, including the Linux Smart Factory
build and its GLB, run from the workspace root:

```powershell
python "Lab Gateway/scripts/sync_fmu_examples.py" --examples-root "FMU-Executor/examples"
```

The Reference FMUs and their bundled source files are licensed under the
2-Clause BSD license in `LICENSE.txt`. The `BinaryClockTest` source is a
DecentraLabs test fixture and is not part of that upstream license.
