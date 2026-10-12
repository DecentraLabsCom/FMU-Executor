from __future__ import annotations

import platform
import zipfile
from pathlib import Path


def write_test_fmu(path: Path, *, model_description: str | None = None) -> Path:
    """Write a small structurally valid FMI 2.0 Co-Simulation archive."""
    model_description = model_description or """<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="TestModel" guid="{test-guid}"
  generationTool="tests" variableNamingConvention="flat" numberOfEventIndicators="0">
  <CoSimulation modelIdentifier="TestModel"/>
  <ModelVariables>
    <ScalarVariable name="x" valueReference="1" causality="input" variability="continuous"><Real start="0"/></ScalarVariable>
    <ScalarVariable name="y" valueReference="2" causality="output" variability="continuous"><Real/></ScalarVariable>
  </ModelVariables>
  <ModelStructure><Outputs><Unknown index="2"/></Outputs></ModelStructure>
</fmiModelDescription>
"""
    if platform.system().lower() == "windows":
        binary_path = "binaries/win64/TestModel.dll"
    elif platform.system().lower() == "darwin":
        binary_path = "binaries/darwin64/TestModel.dylib"
    else:
        binary_path = "binaries/linux64/TestModel.so"

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("modelDescription.xml", model_description)
        binary_directory, binary_filename = binary_path.rsplit("/", 1)
        extension = Path(binary_filename).suffix
        for model_identifier in ("TestModel", "BouncingBall", "Test", "Fmi3Model", "Fmi2Model"):
            archive.writestr(
                f"{binary_directory}/{model_identifier}{extension}",
                b"test-only native binary placeholder",
            )
    return path
