"""FMPy construction helper for FMI 3 Model Exchange FMUs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fmpy.fmi3 import FMU3Model


_FMI3_NON_MODEL_EXCHANGE_FUNCTIONS = frozenset({
    # Co-Simulation only.
    "fmi3DoStep",
    "fmi3EnterStepMode",
    "fmi3GetOutputDerivatives",
    "fmi3InstantiateCoSimulation",
    # Scheduled Execution only.
    "fmi3ActivateModelPartition",
    "fmi3InstantiateScheduledExecution",
})


class _UnavailableFmiFunction:
    def __init__(self, name: str):
        self.name = name

    def __call__(self, *_args: Any) -> Any:
        raise RuntimeError(f"{self.name} is not available on this FMI 3 Model Exchange FMU.")


class _InterfaceAwareLibrary:
    def __init__(self, library: Any):
        self._library = library

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(self._library, name)
        except AttributeError:
            if name in _FMI3_NON_MODEL_EXCHANGE_FUNCTIONS:
                return _UnavailableFmiFunction(name)
            raise


class _Fmi3ModelExchange(FMU3Model):
    """Allow FMPy to bind only symbols required by the selected FMI interface."""

    def _loadFunctions(self) -> None:
        library = self.dll
        self.dll = _InterfaceAwareLibrary(library)
        try:
            super()._loadFunctions()
        finally:
            self.dll = library


def instantiate_fmi3_model_exchange(
    unzip_directory: str | Path,
    model_description: Any,
) -> Any:
    """Instantiate an FMI 3 Model Exchange FMU without requiring other interfaces."""
    model_exchange = getattr(model_description, "modelExchange", None)
    if model_exchange is None:
        raise ValueError("The FMU does not declare FMI 3 Model Exchange")

    model = _Fmi3ModelExchange(
        guid=model_description.guid,
        modelIdentifier=model_exchange.modelIdentifier,
        unzipDirectory=str(unzip_directory),
    )
    model.instantiate(visible=False, loggingOn=False)
    return model
