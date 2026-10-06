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
    available = bool(config.omsimulator_enabled() and shutil.which(command))
    return {
        "id": "omsimulator",
        "enabled": config.omsimulator_enabled(),
        "available": available,
        "commandConfigured": bool(command),
        "supports": ["SSP", "multi-fmu", "ModelExchange", "CoSimulation"] if available else [],
        "state": "available" if available else "planned",
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
