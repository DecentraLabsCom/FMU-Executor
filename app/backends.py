"""Simulation backend capabilities exposed by the Station executor.

FMPy is the current execution backend. OMSimulator is intentionally modelled
as an optional composition backend so SSP/multi-FMU scenarios can be added
without making a native OMSimulator installation a prerequisite for ordinary
single-FMU Station deployments.
"""

from __future__ import annotations

import shutil
from typing import Any

from . import config


def omsimulator_status() -> dict[str, Any]:
    command = config.omsimulator_command()
    installed = bool(shutil.which(command))
    enabled = config.omsimulator_enabled()
    # Discovery alone does not mean the API has an OMSimulator execution
    # adapter. Keep the backend unavailable until requests can really run.
    return {
        "id": "omsimulator",
        "enabled": enabled,
        "installed": installed,
        "executable": False,
        "available": False,
        "adapterImplemented": False,
        "commandConfigured": bool(command),
        "supports": [],
        "state": (
            "disabled" if not enabled
            else "installed_not_executable" if installed
            else "planned"
        ),
    }


def backend_status() -> dict[str, Any]:
    return {
        "fmpy": {
            "id": "fmpy",
            "available": True,
            "supports": ["FMI 2 CoSimulation", "FMI 3 CoSimulation"],
            "state": "active",
        },
        "omsimulator": omsimulator_status(),
    }


def validate_requested_backend(options: dict[str, Any]) -> None:
    backend = str(options.get("backend", "fmpy") or "fmpy").strip().lower()
    if backend in {"fmpy", "default"}:
        return
    if backend == "omsimulator":
        raise NotImplementedError(
            "OMSimulator is prepared as an optional future composition backend; "
            "single-FMU execution currently uses FMPy"
        )
    raise ValueError(f"Unknown FMU execution backend: {backend}")
