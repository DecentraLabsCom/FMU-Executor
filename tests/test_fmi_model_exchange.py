from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import config, fmi_model_exchange
from app.engine import FmuSession
from app.fmi_model_exchange import FmiModelExchangeIntegrator


def test_event_iteration_accumulates_state_and_nominal_change_flags():
    integrator = FmiModelExchangeIntegrator.__new__(FmiModelExchangeIntegrator)
    integrator._fmi_major = "3"
    integrator._fmu = Mock()
    integrator._fmu.updateDiscreteStates.side_effect = [
        (True, False, True, False, False, 0.0),
        (False, False, False, True, False, 0.0),
    ]

    assert integrator._update_discrete_states(0.0) is False
    assert integrator._event_nominals_changed is True
    assert integrator._event_states_changed is True
    assert integrator._next_event_time_defined is False
    assert integrator._next_event_time is None


def test_model_exchange_integrator_accepts_fmi_default_experiment_tolerance_string(monkeypatch):
    solver_arguments = {}

    def create_solver(**kwargs):
        solver_arguments.update(kwargs)
        return object()

    monkeypatch.setattr(fmi_model_exchange, "CVodeSolver", create_solver)
    fmu = Mock()
    fmu.updateDiscreteStates.return_value = (False, False, False, False, False, 0.0)
    model_description = SimpleNamespace(
        fmiVersion="3.0",
        numberOfContinuousStates=1,
        numberOfEventIndicators=0,
    )

    FmiModelExchangeIntegrator(
        fmu,
        model_description,
        start_time=0.0,
        max_step=0.01,
        relative_tolerance="1e-6",
    )

    assert solver_arguments["relativeTolerance"] == 1e-6


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux"} or platform.machine().lower() not in {"amd64", "x86_64"},
    reason="The bundled reference FMUs provide Windows/Linux x86-64 binaries",
)
@pytest.mark.parametrize("model_name", ["BouncingBall", "VanDerPol", "SimpleDAE"])
def test_native_model_exchange_checkpoint_restores_and_continues(model_name, tmp_path, monkeypatch):
    reference = Path(__file__).resolve().parents[1] / "examples" / "reference-fmus" / f"{model_name}.fmu"
    fmu_path = tmp_path / reference.name
    shutil.copyfile(reference, fmu_path)
    extraction_root = tmp_path / "extract"
    extraction_root.mkdir()
    monkeypatch.setattr(config, "FMU_ROOT", tmp_path)
    monkeypatch.setattr(config, "TEMP_DIR", extraction_root)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")

    session = FmuSession(f"checkpoint-{model_name}", fmu_path)
    try:
        session.load()
        initialized = session.initialize(
            start_time=0.0, stop_time=0.2, step_size=0.01, execution_interface="ModelExchange",
        )
        assert initialized["executionInterface"] == "ModelExchange"
        for _ in range(5):
            session.step()
        checkpoint_outputs = session.get_outputs()["outputs"]
        snapshot = session.capture_fmu_state()
        for _ in range(5):
            session.step()
        expected_outputs = session.get_outputs()["outputs"]

        session.restore_fmu_state(snapshot)
        restored = session.get_outputs()
        assert restored["time"] == pytest.approx(0.05)
        assert restored["outputs"]
        for name, value in checkpoint_outputs.items():
            assert restored["outputs"][name] == pytest.approx(value, rel=1e-9, abs=1e-8)

        session.resume()
        for _ in range(5):
            session.step()
        replayed = session.get_outputs()
        assert replayed["time"] == pytest.approx(0.1)
        assert replayed["outputs"].keys() == expected_outputs.keys()
        for name, value in expected_outputs.items():
            # A restored solver restarts its numerical history; compare within
            # integration tolerance rather than requiring bit-identical steps.
            assert replayed["outputs"][name] == pytest.approx(value, rel=1e-3, abs=1e-4)
    finally:
        session.terminate()
