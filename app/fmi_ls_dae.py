"""Bounded FMI-LS-DAE manifest parsing and FMI 3 Model Exchange stepping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import zipfile
import xml.etree.ElementTree as ET
import ctypes
import logging
import math

logger = logging.getLogger(__name__)


MANIFEST_NAME = "org.fmi-standard.fmi-ls-dae"
MANIFEST_PATH = f"extra/{MANIFEST_NAME}/fmi-ls-manifest.xml"
MANIFEST_NAMESPACE = "http://fmi-standard.org/fmi-ls-manifest"
SUPPORTED_MANIFEST_VERSIONS = {"1.0.0-alpha.1"}
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_EQUATIONS = 100_000


@dataclass(frozen=True)
class FmiLsDaeDefinition:
    manifest_version: str
    enable_dae_value_reference: int
    algebraic_value_references: tuple[int, ...]
    residual_value_references: tuple[int, ...]
    algebraic_names: tuple[str, ...]
    residual_names: tuple[str, ...]
    algebraic_types: tuple[str, ...]
    residual_types: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        return {
            "manifestName": MANIFEST_NAME,
            "manifestVersion": self.manifest_version,
            "algebraicVariables": list(self.algebraic_names),
            "residualVariableCount": len(self.residual_value_references),
        }


@dataclass(frozen=True)
class _RealVariableSpec:
    value_reference: int
    value_type: str
    size: int
    offset: int


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _value_reference(element: ET.Element, *, label: str) -> int:
    raw = element.get("valueReference")
    try:
        value = int(raw) if raw is not None else -1
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"FMI-LS-DAE {label} has an invalid valueReference") from exc
    if value < 0:
        raise ValueError(f"FMI-LS-DAE {label} has an invalid valueReference")
    return value


def parse_fmi_ls_dae_manifest(xml_bytes: bytes, model_description: Any) -> FmiLsDaeDefinition:
    """Parse and validate the supported FMI-LS-DAE alpha.1 declarations."""
    if not isinstance(xml_bytes, bytes) or not xml_bytes or len(xml_bytes) > MAX_MANIFEST_BYTES:
        raise ValueError("FMI-LS-DAE manifest exceeds the size limit or is empty")
    if any(token in xml_bytes.lower() for token in (b"<!doctype", b"<!entity")):
        raise ValueError("FMI-LS-DAE manifest must not contain a DTD or entity declaration")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError("FMI-LS-DAE manifest XML is invalid") from exc

    if _local_name(root.tag) != "fmiDAEManifest":
        raise ValueError("FMI-LS-DAE manifest has an unexpected root element")
    manifest_name = root.get(f"{{{MANIFEST_NAMESPACE}}}fmi-ls-name")
    manifest_version = root.get(f"{{{MANIFEST_NAMESPACE}}}fmi-ls-version")
    if manifest_name != MANIFEST_NAME:
        raise ValueError("FMI-LS-DAE manifest name is missing or unsupported")
    if manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ValueError("FMI-LS-DAE manifest version is unsupported")
    if str(getattr(model_description, "fmiVersion", "")).split(".", 1)[0] != "3":
        raise ValueError("FMI-LS-DAE requires FMI 3.0 Model Exchange")
    if getattr(model_description, "modelExchange", None) is None:
        raise ValueError("FMI-LS-DAE requires a Model Exchange interface")

    variables = list(getattr(model_description, "modelVariables", []) or [])
    variables_by_reference: dict[int, list[Any]] = {}
    for variable in variables:
        reference = getattr(variable, "valueReference", None)
        if isinstance(reference, int) and not isinstance(reference, bool):
            variables_by_reference.setdefault(reference, []).append(variable)

    children: dict[str, list[ET.Element]] = {}
    for child in root:
        children.setdefault(_local_name(child.tag), []).append(child)
    enable_nodes = children.get("EnableDAEParameter", [])
    algebraic_nodes_containers = children.get("AlgebraicVariables", [])
    structure_nodes = children.get("ModelStructure", [])
    if len(enable_nodes) != 1 or len(algebraic_nodes_containers) != 1 or len(structure_nodes) != 1:
        raise ValueError("FMI-LS-DAE manifest is missing required declarations")
    enable_node = enable_nodes[0]
    algebraic_node = algebraic_nodes_containers[0]
    structure_node = structure_nodes[0]

    enable_reference = _value_reference(enable_node, label="EnableDAEParameter")
    enable_variable = next(
        (variable for variable in variables_by_reference.get(enable_reference, [])
         if getattr(variable, "type", None) == "Boolean"
         and getattr(variable, "causality", None) == "structuralParameter"),
        None,
    )
    if enable_variable is None:
        raise ValueError("FMI-LS-DAE enable parameter does not reference a Boolean structural parameter")
    if getattr(enable_variable, "variability", None) not in {"fixed", "tunable"}:
        raise ValueError("FMI-LS-DAE enable parameter must be a fixed or tunable structural parameter")

    algebraic_nodes = [node for node in algebraic_node if _local_name(node.tag) == "AlgebraicVariable"]
    residual_nodes = [node for node in structure_node if _local_name(node.tag) == "Residual"]
    if len(algebraic_nodes) > MAX_EQUATIONS or len(residual_nodes) > MAX_EQUATIONS:
        raise ValueError("FMI-LS-DAE equation count exceeds the limit")
    algebraic_references = tuple(_value_reference(node, label="AlgebraicVariable") for node in algebraic_nodes)
    residual_references = tuple(_value_reference(node, label="Residual") for node in residual_nodes)
    if len(set(algebraic_references)) != len(algebraic_references):
        raise ValueError("FMI-LS-DAE algebraic variable references must be unique")
    if len(set(residual_references)) != len(residual_references):
        raise ValueError("FMI-LS-DAE residual references must be unique")

    def resolve_real_variables(
        references: tuple[int, ...],
        label: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        names = []
        types = []
        for reference in references:
            candidates = variables_by_reference.get(reference, [])
            variable = next(
                (candidate for candidate in candidates
                 if getattr(candidate, "type", None) in {"Float32", "Float64"}
                 and getattr(candidate, "variability", None) == "continuous"),
                None,
            )
            if variable is None:
                raise ValueError(
                    f"FMI-LS-DAE {label} must reference continuous Float32 or Float64 variables"
                )
            names.append(str(variable.name))
            types.append(str(variable.type))
        return tuple(names), tuple(types)

    algebraic_names, algebraic_types = resolve_real_variables(algebraic_references, "algebraic variables")
    residual_names, residual_types = resolve_real_variables(residual_references, "residuals")
    if set(algebraic_references).intersection(residual_references):
        raise ValueError("FMI-LS-DAE algebraic and residual variables must be distinct")

    return FmiLsDaeDefinition(
        manifest_version=manifest_version,
        enable_dae_value_reference=enable_reference,
        algebraic_value_references=algebraic_references,
        residual_value_references=residual_references,
        algebraic_names=algebraic_names,
        residual_names=residual_names,
        algebraic_types=algebraic_types,
        residual_types=residual_types,
    )


def read_fmi_ls_dae(path: Path, model_description: Any) -> FmiLsDaeDefinition | None:
    """Read the bounded FMI-LS-DAE manifest from an FMU, if present."""
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist().count(MANIFEST_PATH) == 0:
                return None
            if archive.namelist().count(MANIFEST_PATH) != 1:
                raise ValueError("FMI-LS-DAE manifest path is duplicated")
            entry = archive.getinfo(MANIFEST_PATH)
            if entry.file_size <= 0 or entry.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("FMI-LS-DAE manifest exceeds the size limit or is empty")
            xml_bytes = archive.read(entry)
    except zipfile.BadZipFile as exc:
        raise ValueError("FMI-LS-DAE manifest archive is unreadable") from exc
    return parse_fmi_ls_dae_manifest(xml_bytes, model_description)


class Fmi3DaeIntegrator:
    """Integrate FMI-LS-DAE's semi-explicit index-1 system with SUNDIALS IDAS."""

    IDA_NORMAL = 1
    IDA_YA_YDP_INIT = 1
    SUN_PREC_NONE = 0

    def __init__(
        self,
        fmu: Any,
        definition: FmiLsDaeDefinition,
        *,
        model_description: Any,
        start_time: float,
        relative_tolerance: float | None = None,
        internal_step_limit: int = 10_000,
    ):
        try:
            from sundials4py import core, idas
        except ImportError as exc:
            raise RuntimeError("FMI-LS-DAE execution requires the SUNDIALS IDAS runtime") from exc

        self._core = core
        self._idas = idas
        self._fmu = fmu
        self._definition = definition
        variables_by_name = {
            str(getattr(variable, "name", "")): variable
            for variable in (getattr(model_description, "modelVariables", []) or [])
        }
        variables_by_reference = {
            int(variable.valueReference): variable
            for variable in (getattr(model_description, "modelVariables", []) or [])
            if isinstance(getattr(variable, "valueReference", None), int)
            and not isinstance(getattr(variable, "valueReference", None), bool)
        }
        self._algebraic_variables = self._resolve_variable_specs(
            definition.algebraic_value_references,
            definition.algebraic_names,
            definition.algebraic_types,
            variables_by_name,
            variables_by_reference,
        )
        self._residual_variables = self._resolve_variable_specs(
            definition.residual_value_references,
            definition.residual_names,
            definition.residual_types,
            variables_by_name,
            variables_by_reference,
        )
        self._state_count = int(fmu.getNumberOfContinuousStates())
        self._algebraic_count = sum(spec.size for spec in self._algebraic_variables)
        residual_count = sum(spec.size for spec in self._residual_variables)
        if self._algebraic_count != residual_count:
            raise ValueError(
                "FMI-LS-DAE algebraic and residual arrays must contain the same number of values"
            )
        self._vector_size = self._state_count + self._algebraic_count
        self._internal_step_limit = max(1, int(internal_step_limit))
        if self._state_count < 1 or self._vector_size > MAX_EQUATIONS:
            raise ValueError("FMI-LS-DAE state and algebraic equation count exceeds the supported limit")
        self._relative_tolerance = (
            float(relative_tolerance)
            if isinstance(relative_tolerance, (int, float))
            and not isinstance(relative_tolerance, bool)
            and math.isfinite(float(relative_tolerance))
            and 1e-12 <= float(relative_tolerance) <= 1e-2
            else 1e-6
        )
        self._absolute_tolerance = max(1e-12, self._relative_tolerance * 1e-3)
        self._callback_error: Exception | None = None
        self._closed = False
        self._context = None
        self._ida_view = None
        self._ida_memory = None
        self._state_vector = None
        self._derivative_vector = None
        self._output_state_vector = None
        self._output_derivative_vector = None
        self._id_vector = None
        self._linear_solver = None
        self._matrix = None
        try:
            self._initialize_solver(float(start_time))
        except Exception:
            self.close()
            raise

    def _initialize_solver(self, start_time: float) -> None:
        context_status, self._context = self._core.SUNContext_Create(0)
        self._check_sundials(context_status, "create SUNDIALS context")
        self._ida_view = self._idas.IDACreate(self._context)
        self._ida_memory = self._ida_view.get()
        self._state_vector = self._core.N_VNew_Serial(self._vector_size, self._context)
        self._derivative_vector = self._core.N_VNew_Serial(self._vector_size, self._context)
        self._output_state_vector = self._core.N_VNew_Serial(self._vector_size, self._context)
        self._output_derivative_vector = self._core.N_VNew_Serial(self._vector_size, self._context)
        self._id_vector = self._core.N_VNew_Serial(self._vector_size, self._context)
        self._linear_solver = self._core.SUNLinSol_SPGMR(
            self._state_vector,
            self.SUN_PREC_NONE,
            0,
            self._context,
        )
        id_values = self._core.N_VGetNumpyArray(self._id_vector)
        id_values[: self._state_count] = 1.0
        id_values[self._state_count :] = 0.0

        state_values = self._read_continuous_states()
        algebraic_values = self._read_algebraic_values()
        derivative_values = self._read_continuous_state_derivatives()
        self._write_vector(self._state_vector, [*state_values, *algebraic_values])
        self._write_vector(self._derivative_vector, [*derivative_values, *([0.0] * self._algebraic_count)])

        self._check_sundials(
            self._idas.IDAInit(
                self._ida_memory,
                self._residual,
                start_time,
                self._state_vector,
                self._derivative_vector,
            ),
            "initialize SUNDIALS IDAS",
        )
        self._check_sundials(
            self._idas.IDASetLinearSolver(self._ida_memory, self._linear_solver, None),
            "configure SUNDIALS linear solver",
        )
        self._check_sundials(
            self._idas.IDASetMaxNumSteps(self._ida_memory, self._internal_step_limit),
            "configure SUNDIALS step limit",
        )
        self._check_sundials(
            self._idas.IDASStolerances(self._ida_memory, self._relative_tolerance, self._absolute_tolerance),
            "configure SUNDIALS tolerances",
        )
        self._check_sundials(self._idas.IDASetId(self._ida_memory, self._id_vector), "set DAE variable types")
        self._calculate_consistent_initial_conditions(start_time, 1e-6)

    @property
    def state_count(self) -> int:
        return self._state_count

    def advance(self, current_time: float, target_time: float, *, step_size: float) -> float:
        if self._closed:
            raise RuntimeError("FMI-LS-DAE solver is closed")
        if not math.isfinite(current_time) or not math.isfinite(target_time) or target_time <= current_time:
            raise ValueError("FMI-LS-DAE target time must be finite and advance")
        self._callback_error = None
        self._check_sundials(
            self._idas.IDASetMaxNumSteps(self._ida_memory, self._internal_step_limit),
            "set SUNDIALS step limit",
        )
        self._check_sundials(
            self._idas.IDASetMaxStep(self._ida_memory, max(step_size, 1e-12)),
            "set SUNDIALS maximum step",
        )
        status, reached_time = self._idas.IDASolve(
            self._ida_memory,
            float(target_time),
            self._output_state_vector,
            self._output_derivative_vector,
            self.IDA_NORMAL,
        )
        self._check_callback_and_sundials(status, "integrate FMI-LS-DAE model")
        reached_time = float(reached_time)
        if not math.isfinite(reached_time) or reached_time < current_time - 1e-10 or reached_time > target_time + 1e-10:
            raise RuntimeError("SUNDIALS returned a time outside the requested DAE interval")
        self._copy_vector(self._output_state_vector, self._state_vector)
        self._copy_vector(self._output_derivative_vector, self._derivative_vector)
        self._sync_fmu(reached_time, self._state_vector)
        enter_event_mode, terminate_simulation = self._fmu.completedIntegratorStep(
            noSetFMUStatePriorToCurrentPoint=False,
        )
        if enter_event_mode:
            raise RuntimeError("FMI-LS-DAE does not support event or discontinuity handling in this runtime")
        if terminate_simulation:
            raise RuntimeError("FMI Model Exchange requested termination during DAE integration")
        return reached_time

    def restart_after_input_change(self, current_time: float, *, step_size: float) -> None:
        """Discard IDAS history after a discontinuous input update and restore consistency."""
        if self._closed:
            raise RuntimeError("FMI-LS-DAE solver is closed")
        state_values = self._read_continuous_states()
        algebraic_values = self._read_algebraic_values()
        derivative_values = self._read_continuous_state_derivatives()
        self._write_vector(self._state_vector, [*state_values, *algebraic_values])
        self._write_vector(self._derivative_vector, [*derivative_values, *([0.0] * self._algebraic_count)])
        self._check_sundials(
            self._idas.IDAReInit(self._ida_memory, float(current_time), self._state_vector, self._derivative_vector),
            "restart SUNDIALS after an input change",
        )
        self._calculate_consistent_initial_conditions(current_time, max(1e-8, min(abs(step_size) * 0.01, 1e-3)))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for operation, value in (
            (getattr(self._idas, "IDAFree", None), self._ida_memory),
            (getattr(self._core, "SUNLinSolFree", None), self._linear_solver),
            (getattr(self._core, "N_VDestroy", None), self._id_vector),
            (getattr(self._core, "N_VDestroy", None), self._output_derivative_vector),
            (getattr(self._core, "N_VDestroy", None), self._output_state_vector),
            (getattr(self._core, "N_VDestroy", None), self._derivative_vector),
            (getattr(self._core, "N_VDestroy", None), self._state_vector),
        ):
            if operation is not None and value is not None:
                try:
                    operation(value)
                except Exception:
                    logger.debug("Could not release SUNDIALS allocation", exc_info=True)
        context_free = getattr(self._core, "SUNContext_Free", None)
        if context_free is not None and self._context is not None:
            try:
                context_free(self._context)
            except Exception:
                logger.debug("Could not release SUNDIALS context", exc_info=True)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _calculate_consistent_initial_conditions(self, current_time: float, interval: float) -> None:
        self._callback_error = None
        status = self._idas.IDACalcIC(
            self._ida_memory,
            self.IDA_YA_YDP_INIT,
            float(current_time + interval),
        )
        self._check_callback_and_sundials(status, "calculate consistent DAE initial conditions")
        status = self._idas.IDAGetConsistentIC(
            self._ida_memory,
            self._state_vector,
            self._derivative_vector,
        )
        self._check_sundials(status, "read consistent DAE initial conditions")
        self._sync_fmu(current_time, self._state_vector)

    def _residual(self, time: float, state_vector: Any, derivative_vector: Any, residual_vector: Any, _user_data: Any) -> int:
        try:
            y = self._core.N_VGetNumpyArray(state_vector)
            yp = self._core.N_VGetNumpyArray(derivative_vector)
            residuals = self._core.N_VGetNumpyArray(residual_vector)
            self._set_fmu_for_solver_point(float(time), y)
            derivatives = self._read_continuous_state_derivatives()
            residuals[: self._state_count] = yp[: self._state_count] - derivatives
            if self._algebraic_count:
                residuals[self._state_count :] = self._read_real_values(self._residual_variables)
            return 0
        except Exception as exc:
            self._callback_error = exc
            return -1

    def _set_fmu_for_solver_point(self, time: float, values: Any) -> None:
        self._fmu.setTime(float(time))
        state_values = (ctypes.c_double * self._state_count)(
            *(float(value) for value in values[: self._state_count])
        )
        if self._state_count:
            self._fmu.setContinuousStates(state_values, self._state_count)
        if self._algebraic_count:
            self._write_real_values(self._algebraic_variables, values[self._state_count :].tolist())

    def _sync_fmu(self, time: float, state_vector: Any) -> None:
        self._set_fmu_for_solver_point(time, self._core.N_VGetNumpyArray(state_vector))

    def _read_continuous_states(self) -> list[float]:
        if not self._state_count:
            return []
        values = (ctypes.c_double * self._state_count)()
        self._fmu.getContinuousStates(values, self._state_count)
        return [float(value) for value in values]

    def _read_continuous_state_derivatives(self) -> list[float]:
        if not self._state_count:
            return []
        values = (ctypes.c_double * self._state_count)()
        self._fmu.getContinuousStateDerivatives(values, self._state_count)
        return [float(value) for value in values]

    def _read_algebraic_values(self) -> list[float]:
        if not self._algebraic_count:
            return []
        return self._read_real_values(self._algebraic_variables)

    @staticmethod
    def _resolve_variable_specs(
        references: tuple[int, ...],
        names: tuple[str, ...],
        types: tuple[str, ...],
        variables_by_name: dict[str, Any],
        variables_by_reference: dict[int, Any],
    ) -> tuple[_RealVariableSpec, ...]:
        specs = []
        offset = 0
        for reference, name, value_type in zip(references, names, types, strict=True):
            variable = variables_by_name.get(name)
            if (
                variable is None
                or getattr(variable, "type", None) != value_type
                or getattr(variable, "valueReference", None) != reference
            ):
                raise ValueError(f"FMI-LS-DAE variable '{name}' is missing from the model description")
            size = Fmi3DaeIntegrator._variable_size(variable, variables_by_reference)
            if size > MAX_EQUATIONS or offset + size > MAX_EQUATIONS:
                raise ValueError("FMI-LS-DAE array equation count exceeds the supported limit")
            specs.append(_RealVariableSpec(reference, value_type, size, offset))
            offset += size
        return tuple(specs)

    @staticmethod
    def _variable_size(variable: Any, variables_by_reference: dict[int, Any]) -> int:
        shape = getattr(variable, "shape", None)
        if isinstance(shape, (list, tuple)) and shape:
            extents = shape
        else:
            dimensions = getattr(variable, "dimensions", None)
            if not isinstance(dimensions, (list, tuple)) or not dimensions:
                return 1
            extents = []
            for dimension in dimensions:
                extent = getattr(dimension, "start", None)
                if extent is None:
                    referenced_variable = getattr(dimension, "variable", None)
                    extent = getattr(referenced_variable, "start", None)
                if extent is None:
                    reference = getattr(dimension, "valueReference", None)
                    if isinstance(reference, int) and not isinstance(reference, bool):
                        referenced_variable = variables_by_reference.get(reference)
                        extent = getattr(referenced_variable, "start", None)
                extents.append(extent)

        size = 1
        for extent in extents:
            if isinstance(extent, bool):
                raise ValueError("FMI-LS-DAE array has an invalid dimension")
            try:
                normalized_extent = int(extent)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("FMI-LS-DAE array has an unresolved dimension") from exc
            if normalized_extent < 0 or normalized_extent != extent:
                raise ValueError("FMI-LS-DAE array has an invalid dimension")
            size *= normalized_extent
            if size > MAX_EQUATIONS:
                raise ValueError("FMI-LS-DAE array equation count exceeds the supported limit")
        return size

    def _read_real_values(self, specs: tuple[_RealVariableSpec, ...]) -> list[float]:
        result = [0.0] * sum(spec.size for spec in specs)
        for value_type in ("Float32", "Float64"):
            group = [spec for spec in specs if spec.value_type == value_type and spec.size > 0]
            if not group:
                continue
            total_size = sum(spec.size for spec in group)
            getter = getattr(self._fmu, f"get{value_type}")
            values = getter(
                [spec.value_reference for spec in group],
                nValues=total_size,
            )
            source_offset = 0
            for spec in group:
                result[spec.offset : spec.offset + spec.size] = [
                    float(value)
                    for value in values[source_offset : source_offset + spec.size]
                ]
                source_offset += spec.size
        return result

    def _write_real_values(
        self,
        specs: tuple[_RealVariableSpec, ...],
        values: list[float],
    ) -> None:
        for value_type in ("Float32", "Float64"):
            group = [spec for spec in specs if spec.value_type == value_type and spec.size > 0]
            if not group:
                continue
            flattened = [
                float(value)
                for spec in group
                for value in values[spec.offset : spec.offset + spec.size]
            ]
            setter = getattr(self._fmu, f"set{value_type}")
            setter(
                [spec.value_reference for spec in group],
                flattened,
            )

    def _write_vector(self, vector: Any, values: list[float]) -> None:
        self._core.N_VGetNumpyArray(vector)[:] = values

    def _copy_vector(self, source: Any, target: Any) -> None:
        self._core.N_VGetNumpyArray(target)[:] = self._core.N_VGetNumpyArray(source)

    def _check_callback_and_sundials(self, status: int, operation: str) -> None:
        if self._callback_error is not None:
            error = self._callback_error
            self._callback_error = None
            raise RuntimeError(f"{operation} failed in the FMU equation callback") from error
        self._check_sundials(status, operation)

    @staticmethod
    def _check_sundials(status: int, operation: str) -> None:
        if isinstance(status, bool) or not isinstance(status, int) or status < 0:
            raise RuntimeError(f"SUNDIALS could not {operation} (status {status})")
