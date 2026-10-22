# FMI reference and test FMUs

This directory holds six distributed FMI examples for development and
validation. Their `.fmu` archives are tracked in Git and copied by Gateway's
normal example sync. Windows and Linux x86_64 Lab Station releases include all
six archives with the license; Linux arm64 releases include the three models
with native aarch64 binaries. The five upstream reference models are
unmodified FMU artifacts from
[Modelica Reference FMUs v0.0.39](https://github.com/modelica/Reference-FMUs/tree/v0.0.39):

| FMU | FMI | Linux x86_64 archive platform |
| --- | --- | --- |
| `BouncingBall.fmu` | 2.0 | `linux64` |
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

To refresh the six reference FMUs and Smart Warehouse in Gateway's local
development data directory, run from the workspace root:

```powershell
python "Lab Gateway/scripts/sync_fmu_examples.py" --examples-root "FMU-Executor/examples"
```

A clean checkout contains these six tracked FMU archives. The five upstream
files come from the pinned Modelica Reference FMUs release and
`BinaryClockTest.fmu` can be rebuilt from
the source and scripts below.

The Reference FMUs and their bundled source files are licensed under the
2-Clause BSD license in `LICENSE.txt`. The `BinaryClockTest` source is a
DecentraLabs test fixture and is not part of that upstream license.
