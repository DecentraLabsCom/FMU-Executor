# BinaryClockTest

This DecentraLabs FMI 3.0 Co-Simulation fixture covers Binary, Clock and
Float64 variables. `src/BinaryClockTest.c` and `modelDescription.xml` are the
canonical source.

The canonical `../BinaryClockTest.fmu` is a multi-platform archive. Build the
Windows x64 or Linux x86_64 shared library independently; the packaging helper
replaces only that platform's binary and preserves the other one:

```powershell
./build.ps1
```

```bash
bash ./build-linux.sh
```

On Windows, run the Linux build in Docker from the workspace root:

```powershell
$sourceRoot = (Resolve-Path 'FMU-Executor/examples/reference-fmus').Path.Replace('\','/')
docker run --rm --mount "type=bind,source=$sourceRoot,target=/workspace" `
  --workdir /workspace/BinaryClockTest python:3.13-slim-bookworm sh -lc `
  "apt-get update && apt-get install -y --no-install-recommends build-essential && bash ./build-linux.sh"
```

The Linux platform directory is `binaries/x86_64-linux/`, following the FMI 3
platform naming used by the other FMI 3 examples. Refresh the Gateway copies
from the workspace root with:

```powershell
python "Lab Gateway/scripts/sync_fmu_examples.py" --examples-root "FMU-Executor/examples"
```
