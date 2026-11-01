"""FMPy construction helpers for FMI 3 Co-Simulation FMUs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fmpy.fmi3 import FMU3Slave


_FMI3_NON_COSIMULATION_FUNCTIONS = frozenset({
    # Scheduled Execution only.
    "fmi3ActivateModelPartition",
    "fmi3InstantiateScheduledExecution",
    # Model Exchange only.
    "fmi3CompletedIntegratorStep",
    "fmi3EnterContinuousTimeMode",
    "fmi3GetContinuousStateDerivatives",
    "fmi3GetContinuousStates",
    "fmi3GetEventIndicators",
    "fmi3GetNominalsOfContinuousStates",
    "fmi3GetNumberOfContinuousStates",
    "fmi3GetNumberOfEventIndicators",
    "fmi3InstantiateModelExchange",
    "fmi3SetContinuousStates",
    "fmi3SetTime",
})


class _UnavailableFmiFunction:
    """Placeholder FMPy can bind for an API function outside the selected type."""

    def __init__(self, name: str):
        self.name = name

    def __call__(self, *_args: Any) -> Any:
        raise RuntimeError(
            f"{self.name} is not available on this FMI 3 Co-Simulation FMU."
        )


class _InterfaceAwareLibrary:
    """Treat absent Model Exchange/Scheduled Execution symbols as non-applicable."""

    def __init__(self, library: Any):
        self._library = library

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(self._library, name)
        except AttributeError:
            if name in _FMI3_NON_COSIMULATION_FUNCTIONS:
                return _UnavailableFmiFunction(name)
            raise


class _Fmi3CoSimulationSlave(FMU3Slave):
    """Load only symbols applicable to the FMI 3 Co-Simulation interface.

    FMPy 0.3.32 reflects over the common FMI 3 API class and attempts to bind
    functions from Model Exchange and Scheduled Execution even when the FMU
    declares only Co-Simulation. Keep FMPy's binding logic for actual symbols,
    while allowing its methods for other interface types to remain unavailable.
    """

    def _loadFunctions(self) -> None:
        library = self.dll
        self.dll = _InterfaceAwareLibrary(library)
        try:
            super()._loadFunctions()
        finally:
            self.dll = library


def instantiate_fmi3_cosimulation(
    unzip_directory: str | Path,
    model_description: Any,
    *,
    early_return_allowed: bool = False,
    event_mode_used: bool = False,
) -> Any:
    """Instantiate an FMI 3 Co-Simulation FMU without requiring other FMI APIs."""

    co_simulation = getattr(model_description, "coSimulation", None)
    if co_simulation is None:
        raise ValueError("The FMU does not declare FMI 3 Co-Simulation")

    slave = _Fmi3CoSimulationSlave(
        guid=model_description.guid,
        modelIdentifier=co_simulation.modelIdentifier,
        unzipDirectory=str(unzip_directory),
    )
    slave.instantiate(
        visible=False,
        loggingOn=False,
        eventModeUsed=event_mode_used,
        earlyReturnAllowed=early_return_allowed,
    )
    return slave
