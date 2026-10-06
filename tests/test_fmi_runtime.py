"""Focused tests for the generic FMI 2/FMI 3 Station runtime."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _variable(name: str, value_reference: int, variable_type: str, causality: str, *, shape=None):
    variable = MagicMock()
    variable.name = name
    variable.valueReference = value_reference
    variable.type = variable_type
    variable.causality = causality
    variable.shape = shape
    variable.dimensions = []
    return variable


def _fmi3_model_description():
    model = MagicMock()
    model.fmiVersion = "3.0"
    model.guid = "{fmi3-guid}"
    model.coSimulation = MagicMock(modelIdentifier="Fmi3Model")
    model.defaultExperiment = None
    model.modelVariables = [
        _variable("vectorInput", 1, "Float32", "input", shape=(2,)),
        _variable("vectorOutput", 2, "Float64", "output", shape=(2,)),
        _variable("largeCounter", 3, "UInt64", "output"),
        _variable("payload", 4, "Binary", "output"),
    ]
    return model


def test_fmi3_cosimulation_uses_generic_instantiation_and_arrays(tmp_path: Path):
    from app import engine

    model = _fmi3_model_description()
    fmu = MagicMock()
    fmu.getFloat64.return_value = [1.25, 2.5]
    fmu.getUInt64.return_value = [2**40]
    fmu.getBinary.return_value = [b"\x01\x02"]

    session = engine.FmuSession("fmi3-test", tmp_path / "model.fmu")
    session._extract_dir = tmp_path
    session._md = model
    with patch("app.engine.fmpy_instantiate_fmu", return_value=fmu) as instantiate:
        result = session.initialize(
            start_time=0.0,
            stop_time=2.0,
            step_size=0.1,
            parameters={"vectorInput": [1, 2]},
        )

    instantiate.assert_called_once_with(str(tmp_path), model, fmi_type="CoSimulation")
    fmu.enterInitializationMode.assert_called_once_with(startTime=0.0, stopTime=2.0)
    fmu.setFloat32.assert_called_once_with([1], [1.0, 2.0])
    assert result["state"] == "initialized"

    outputs = session.get_outputs()["outputs"]
    assert outputs["vectorOutput"] == [1.25, 2.5]
    assert outputs["largeCounter"] == str(2**40)
    assert outputs["payload"] == "AQI="


def test_fmi_session_lifecycle_supports_pause_resume_and_reset(tmp_path: Path):
    from app import engine

    model = MagicMock()
    model.fmiVersion = "2.0"
    model.guid = "{fmi2-guid}"
    model.coSimulation = MagicMock(modelIdentifier="Fmi2Model")
    model.defaultExperiment = None
    model.modelVariables = []
    fmu = MagicMock()

    session = engine.FmuSession("lifecycle-test", tmp_path / "model.fmu")
    session._extract_dir = tmp_path
    session._md = model
    with patch("app.engine.fmpy_instantiate_fmu", return_value=fmu) as instantiate:
        session.initialize(stop_time=1.0, step_size=0.1)
        session.step()
        assert session.pause()["state"] == "paused"
        assert session.resume()["state"] == "running"
        session.reset()

    assert instantiate.call_count == 2
    assert session._time == 0.0
    assert session.state == "initialized"


def test_array_inputs_require_the_declared_flat_size(tmp_path: Path):
    from app import engine

    variable = _variable("input", 1, "Float64", "input", shape=(2,))
    with pytest.raises(ValueError, match="expects 2 values"):
        engine.FmuSession._coerce_values(variable, [1.0])
