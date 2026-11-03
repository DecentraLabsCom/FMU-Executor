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

from .fmi3_clock_scheduler import (
    Fmi3InputClockSchedule,
    consume_fmi3_clock_ticks,
    due_fmi3_clock_references,
    refresh_fmi3_clock_schedules,
)

logger = logging.getLogger(__name__)


MANIFEST_NAME = "org.fmi-standard.fmi-ls-dae"
MANIFEST_PATH = f"extra/{MANIFEST_NAME}/fmi-ls-manifest.xml"
MANIFEST_NAMESPACE = "http://fmi-standard.org/fmi-ls-manifest"
SUPPORTED_MANIFEST_VERSIONS = {"1.0.0-alpha.1"}
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_EQUATIONS = 100_000
MAX_FMI3_VALUE_REFERENCE = (1 << 32) - 1
DEPENDENCY_KINDS = frozenset({"dependent", "constant", "fixed", "tunable", "discrete"})
NUMERIC_VARIABLE_TYPES = frozenset({
    "Float32", "Float64", "Int8", "UInt8", "Int16", "UInt16",
    "Int32", "UInt32", "Int64", "UInt64", "Enumeration",
})


@dataclass(frozen=True)
class FmiLsDaeModelStructureEntry:
    kind: str
    value_reference: int
    dependencies: tuple[int, ...] | None
    dependencies_kind: tuple[str, ...] | None


@dataclass(frozen=True)
class FmiLsDaeDefinition:
    manifest_version: str
    enable_dae_value_reference: int
    enable_dae_variable_name: str
    algebraic_value_references: tuple[int, ...]
    residual_value_references: tuple[int, ...]
    algebraic_names: tuple[str, ...]
    residual_names: tuple[str, ...]
    algebraic_types: tuple[str, ...]
    residual_types: tuple[str, ...]
    derivative_value_references: tuple[int, ...]
    derivative_names: tuple[str, ...]
    derivative_types: tuple[str, ...]
    state_value_references: tuple[int, ...]
    model_structure: tuple[FmiLsDaeModelStructureEntry, ...]

    @property
    def output_value_references(self) -> tuple[int, ...]:
        return tuple(
            entry.value_reference for entry in self.model_structure
            if entry.kind == "Output"
        )

    @property
    def initial_unknown_value_references(self) -> tuple[int, ...]:
        return tuple(
            entry.value_reference for entry in self.model_structure
            if entry.kind == "InitialUnknown"
        )

    @property
    def event_indicator_value_references(self) -> tuple[int, ...]:
        return tuple(
            entry.value_reference for entry in self.model_structure
            if entry.kind == "EventIndicator"
        )

    @property
    def clocked_state_value_references(self) -> tuple[int, ...]:
        return tuple(
            entry.value_reference for entry in self.model_structure
            if entry.kind == "ClockedState"
        )

    @property
    def residual_model_structure(self) -> tuple[FmiLsDaeModelStructureEntry, ...]:
        return tuple(entry for entry in self.model_structure if entry.kind == "Residual")

    def describe(self) -> dict[str, Any]:
        return {
            "manifestName": MANIFEST_NAME,
            "manifestVersion": self.manifest_version,
            "enableDAEParameter": self.enable_dae_variable_name,
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
    if value < 0 or value > MAX_FMI3_VALUE_REFERENCE:
        raise ValueError(f"FMI-LS-DAE {label} has an invalid valueReference")
    return value


def _validate_annotations_only(element: ET.Element, *, label: str, allowed_attributes: set[str]) -> None:
    if set(element.attrib) - allowed_attributes:
        raise ValueError(f"FMI-LS-DAE {label} contains unsupported attributes")
    annotations = [child for child in element if _local_name(child.tag) == "Annotations"]
    if len(annotations) > 1 or len(annotations) != len(list(element)):
        raise ValueError(f"FMI-LS-DAE {label} contains unsupported child elements")


def _parse_dependency_references(
    raw: str | None,
    *,
    label: str,
    variables_by_reference: dict[int, list[Any]],
) -> tuple[int, ...] | None:
    if raw is None:
        return None
    tokens = raw.split()
    references: list[int] = []
    for token in tokens:
        try:
            reference = int(token)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"FMI-LS-DAE {label} has an invalid dependency valueReference") from exc
        if reference < 0 or reference > MAX_FMI3_VALUE_REFERENCE:
            raise ValueError(f"FMI-LS-DAE {label} has an invalid dependency valueReference")
        if reference not in variables_by_reference:
            raise ValueError(f"FMI-LS-DAE {label} references unknown dependency valueReference {reference}")
        references.append(reference)
    if len(references) != len(set(references)):
        raise ValueError(f"FMI-LS-DAE {label} dependency references must be unique")
    return tuple(references)


def _model_variable_size(variable: Any, variables_by_reference: dict[int, Any]) -> int:
    """Resolve the declared scalar/array extent used by FMI ModelStructure lists."""
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
                    extent = getattr(variables_by_reference.get(reference), "start", None)
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


def _parse_model_structure(
    structure_node: ET.Element,
    variables: list[Any],
    variables_by_reference: dict[int, list[Any]],
    algebraic_references: set[int],
) -> tuple[FmiLsDaeModelStructureEntry, ...]:
    order = {
        "Output": 0,
        "ContinuousStateDerivative": 1,
        "ClockedState": 2,
        "InitialUnknown": 3,
        "EventIndicator": 4,
        "Residual": 5,
    }
    entries: list[FmiLsDaeModelStructureEntry] = []
    structure_children = list(structure_node)
    continuous_state_references: set[int] = set()
    continuous_derivative_references: set[int] = set()
    for node in structure_children:
        if node.tag != "ContinuousStateDerivative":
            continue
        derivative_reference = _value_reference(node, label="ModelStructure/ContinuousStateDerivative")
        for variable in variables_by_reference.get(derivative_reference, []):
            derivative_of = getattr(variable, "derivative", None)
            if derivative_of is None:
                continue
            continuous_derivative_references.add(derivative_reference)
            state_reference = getattr(derivative_of, "valueReference", derivative_of)
            if isinstance(state_reference, int) and not isinstance(state_reference, bool):
                continuous_state_references.add(state_reference)
    declared_event_indicator_references = {
        _value_reference(node, label="ModelStructure/EventIndicator")
        for node in structure_children
        if node.tag == "EventIndicator"
    }
    input_parameter_and_time_references = {
        reference
        for reference, candidates in variables_by_reference.items()
        if any(
            getattr(variable, "causality", None) in {"input", "parameter", "independent"}
            for variable in candidates
        )
    }
    discrete_state_references = {
        reference
        for reference, candidates in variables_by_reference.items()
        if any(
            getattr(variable, "variability", None) == "discrete"
            and getattr(variable, "causality", None) != "input"
            and getattr(variable, "type", None) != "Clock"
            for variable in candidates
        )
    }
    clock_references = {
        reference
        for reference, candidates in variables_by_reference.items()
        if any(getattr(variable, "type", None) == "Clock" for variable in candidates)
    }
    # FMI 3 knowns for outputs/derivatives are inputs, continuous and
    # discrete states, parameters and the independent variable. FMI-LS-DAE
    # additionally permits algebraic variables for the DAE equations.
    runtime_known_references = (
        continuous_state_references
        | discrete_state_references
        | input_parameter_and_time_references
    )
    initial_known_references = set(algebraic_references) | {
        reference
        for reference, candidates in variables_by_reference.items()
        if any(
            getattr(variable, "causality", None) in {"input", "independent"}
            or getattr(variable, "initial", None) == "exact"
            for variable in candidates
        )
    }
    previous_order = -1
    seen_by_kind: dict[str, set[int]] = {}
    clocked_state_variables_by_reference: dict[int, Any] = {}
    for node in structure_children:
        kind = node.tag
        if kind not in order:
            raise ValueError(f"FMI-LS-DAE contains unsupported ModelStructure element {kind!r}")
        current_order = order[kind]
        if current_order < previous_order:
            raise ValueError("FMI-LS-DAE has invalid ModelStructure order")
        previous_order = current_order
        _validate_annotations_only(
            node,
            label=f"ModelStructure/{kind}",
            allowed_attributes={"valueReference", "dependencies", "dependenciesKind"},
        )
        reference = _value_reference(node, label=f"ModelStructure/{kind}")
        candidates = variables_by_reference.get(reference)
        if not candidates:
            raise ValueError(f"FMI-LS-DAE {kind} references an unknown model variable")
        seen = seen_by_kind.setdefault(kind, set())
        if reference in seen:
            raise ValueError(f"FMI-LS-DAE {kind} references must be unique")
        seen.add(reference)

        dependencies = _parse_dependency_references(
            node.get("dependencies"),
            label=kind,
            variables_by_reference=variables_by_reference,
        )
        raw_kinds = node.get("dependenciesKind")
        dependencies_kind: tuple[str, ...] | None = None
        if raw_kinds is not None:
            if dependencies is None:
                raise ValueError(f"FMI-LS-DAE {kind} dependenciesKind requires dependencies")
            parsed_kinds = tuple(raw_kinds.split())
            if len(parsed_kinds) != len(dependencies):
                raise ValueError(f"FMI-LS-DAE {kind} dependenciesKind count does not match dependencies")
            allowed_kinds = {"dependent", "constant"} if kind == "InitialUnknown" else DEPENDENCY_KINDS
            if any(value not in allowed_kinds for value in parsed_kinds):
                raise ValueError(f"FMI-LS-DAE {kind} has an unsupported dependenciesKind")
            if any(value != "dependent" for value in parsed_kinds) and not all(
                getattr(candidate, "type", None) in {"Float32", "Float64"}
                for candidate in candidates
            ):
                raise ValueError(f"FMI-LS-DAE {kind} dependenciesKind requires a floating-point unknown")
            dependencies_kind = parsed_kinds

        numeric_continuous = any(
            getattr(candidate, "type", None) in {"Float32", "Float64"}
            and getattr(candidate, "variability", None) == "continuous"
            for candidate in candidates
        )
        if kind in {"ContinuousStateDerivative", "EventIndicator", "Residual"} and not numeric_continuous:
            raise ValueError(f"FMI-LS-DAE {kind} must reference a continuous Float32 or Float64 variable")
        if kind == "ContinuousStateDerivative" and not any(
            getattr(candidate, "derivative", None) is not None for candidate in candidates
        ):
            raise ValueError("FMI-LS-DAE ContinuousStateDerivative must reference a derivative variable")
        if kind == "Output" and not any(
            getattr(candidate, "causality", None) == "output" for candidate in candidates
        ):
            raise ValueError("FMI-LS-DAE Output must reference an output variable")
        if kind == "ClockedState":
            valid_clocked_state = False
            validated_clocked_state = None
            for candidate in candidates:
                previous = getattr(candidate, "previous", None)
                previous_reference = getattr(previous, "valueReference", previous)
                clocks = getattr(candidate, "clocks", None)
                candidate_reference = getattr(candidate, "valueReference", None)
                candidate_clock_references = [
                    getattr(clock, "valueReference", clock)
                    for clock in (clocks if isinstance(clocks, (list, tuple)) else [])
                ]
                if (
                    getattr(candidate, "variability", None) != "discrete"
                    or getattr(candidate, "clocks", None) is None
                    or getattr(candidate, "type", None) not in NUMERIC_VARIABLE_TYPES
                    or isinstance(previous_reference, bool)
                    or not isinstance(previous_reference, int)
                    or previous_reference not in variables_by_reference
                    or previous_reference == candidate_reference
                    or not isinstance(clocks, (list, tuple))
                    or not clocks
                    or any(
                        isinstance(clock_reference, bool)
                        or not isinstance(clock_reference, int)
                        or clock_reference not in variables_by_reference
                        or not any(
                            getattr(clock_variable, "type", None) == "Clock"
                            for clock_variable in variables_by_reference[clock_reference]
                        )
                        for clock_reference in candidate_clock_references
                    )
                ):
                    continue
                previous_variables = variables_by_reference[previous_reference]
                if not any(
                    getattr(previous_variable, "type", None) == getattr(candidate, "type", None)
                    and getattr(previous_variable, "variability", None) == "discrete"
                    and getattr(previous_variable, "causality", None) != "input"
                    for previous_variable in previous_variables
                ):
                    continue
                valid_clocked_state = True
                validated_clocked_state = candidate
                break
            if not valid_clocked_state or validated_clocked_state is None:
                raise ValueError(
                    "FMI-LS-DAE ClockedState must reference a discrete variable with a valid previous value and Clock"
                )
            clocked_state_variables_by_reference[reference] = validated_clocked_state
        if kind == "InitialUnknown":
            if reference in declared_event_indicator_references:
                raise ValueError("FMI-LS-DAE EventIndicator must not be listed as an InitialUnknown")
            valid_initial_unknown = any(
                (
                    reference in algebraic_references
                    or getattr(candidate, "causality", None) == "calculatedParameter"
                    or (
                        getattr(candidate, "variability", None) in {"discrete", "tunable"}
                        and bool(getattr(candidate, "clocks", None))
                    )
                    or (
                        getattr(candidate, "initial", None) in {"approx", "calculated"}
                        and (
                            getattr(candidate, "causality", None) == "output"
                            or reference in continuous_state_references
                            or reference in continuous_derivative_references
                        )
                    )
                )
                for candidate in candidates
            )
            if not valid_initial_unknown:
                raise ValueError("FMI-LS-DAE InitialUnknown does not reference an initialization unknown")
        allowed_dependencies = runtime_known_references | set(algebraic_references)
        if kind == "InitialUnknown":
            allowed_dependencies = initial_known_references
        if kind == "ClockedState":
            state_variable = clocked_state_variables_by_reference[reference]
            previous = getattr(state_variable, "previous", None)
            previous_reference = getattr(previous, "valueReference", previous)
            state_clocks = getattr(state_variable, "clocks", None)
            state_clock_references = {
                getattr(clock, "valueReference", clock)
                for clock in (state_clocks if isinstance(state_clocks, (list, tuple)) else [])
                if isinstance(getattr(clock, "valueReference", clock), int)
                and not isinstance(getattr(clock, "valueReference", clock), bool)
            }
            if isinstance(previous_reference, int) and not isinstance(previous_reference, bool):
                state_clock_references.add(previous_reference)
            allowed_dependencies = runtime_known_references | set(algebraic_references) | state_clock_references
        if kind == "Output" and any(getattr(candidate, "type", None) == "Clock" for candidate in candidates):
            allowed_dependencies = runtime_known_references | clock_references | set(algebraic_references)
        if kind == "Residual":
            allowed_dependencies = (
                set(algebraic_references)
                | continuous_state_references
                | input_parameter_and_time_references
            )
        if dependencies is not None and set(dependencies) - allowed_dependencies:
            raise ValueError(
                f"FMI-LS-DAE {kind} dependencies must reference known variables in that ModelStructure mode"
            )
        if kind == "ClockedState" and dependencies is not None:
            state_variable = clocked_state_variables_by_reference[reference]
            state_clocks = getattr(state_variable, "clocks", None)
            required_clocks = {
                getattr(clock, "valueReference", clock)
                for clock in (state_clocks if isinstance(state_clocks, (list, tuple)) else [])
            }
            if not required_clocks.issubset(set(dependencies)):
                raise ValueError("FMI-LS-DAE ClockedState dependencies must include its associated Clocks")
        entries.append(FmiLsDaeModelStructureEntry(kind, reference, dependencies, dependencies_kind))

    declared_clocked_state_references = seen_by_kind.get("ClockedState", set())
    previous_reference_by_state: dict[int, int] = {}
    for state_reference, candidates in variables_by_reference.items():
        for candidate in candidates:
            previous = getattr(candidate, "previous", None)
            if previous is None:
                continue
            previous_reference = getattr(previous, "valueReference", previous)
            if isinstance(previous_reference, bool) or not isinstance(previous_reference, int):
                raise ValueError("FMI clocked-state previous reference is invalid")
            prior_state_reference = previous_reference_by_state.get(previous_reference)
            if prior_state_reference is not None and prior_state_reference != state_reference:
                raise ValueError("FMI clocked-state previous references must be unique")
            previous_reference_by_state[previous_reference] = state_reference
    model_clocked_state_references = set(previous_reference_by_state.values())
    if declared_clocked_state_references != model_clocked_state_references:
        raise ValueError("FMI-LS-DAE ModelStructure must declare every clocked state exactly once")

    declared_initial_unknown_references = seen_by_kind.get("InitialUnknown", set())
    required_initial_unknown_references: set[int] = set()
    state_or_derivative_references = continuous_state_references | continuous_derivative_references
    for reference, candidates in variables_by_reference.items():
        if reference in declared_event_indicator_references:
            continue
        for variable in candidates:
            causality = getattr(variable, "causality", None)
            if causality == "calculatedParameter":
                required_initial_unknown_references.add(reference)
                continue
            if (
                causality == "output"
                and not bool(getattr(variable, "clocks", None))
                and getattr(variable, "initial", None) in {"approx", "calculated"}
            ) or (
                reference in state_or_derivative_references
                and getattr(variable, "initial", None) in {"approx", "calculated"}
            ):
                required_initial_unknown_references.add(reference)
    missing_initial_unknowns = required_initial_unknown_references - declared_initial_unknown_references
    if missing_initial_unknowns:
        missing_references = ", ".join(str(reference) for reference in sorted(missing_initial_unknowns))
        raise ValueError(
            "FMI-LS-DAE ModelStructure omits required InitialUnknown valueReferences: "
            f"{missing_references}"
        )

    return tuple(entries)


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

    if root.tag != "fmiDAEManifest":
        raise ValueError("FMI-LS-DAE manifest has an unexpected root element")
    manifest_name = root.get(f"{{{MANIFEST_NAMESPACE}}}fmi-ls-name")
    manifest_version = root.get(f"{{{MANIFEST_NAMESPACE}}}fmi-ls-version")
    manifest_description = root.get(f"{{{MANIFEST_NAMESPACE}}}fmi-ls-description")
    if manifest_name != MANIFEST_NAME:
        raise ValueError("FMI-LS-DAE manifest name is missing or unsupported")
    if manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ValueError("FMI-LS-DAE manifest version is unsupported")
    if manifest_description != "Layered standard for DAE support in FMI.":
        raise ValueError("FMI-LS-DAE manifest description is missing or unsupported")
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

    root_children = list(root)
    child_names = [child.tag for child in root_children]
    if child_names not in (
        ["EnableDAEParameter", "AlgebraicVariables", "ModelStructure"],
        ["EnableDAEParameter", "AlgebraicVariables", "ModelStructure", "Annotations"],
    ):
        raise ValueError("FMI-LS-DAE manifest is missing required declarations")
    enable_node, algebraic_node, structure_node = root_children[:3]
    _validate_annotations_only(enable_node, label="EnableDAEParameter", allowed_attributes={"valueReference"})
    if set(algebraic_node.attrib):
        raise ValueError("FMI-LS-DAE AlgebraicVariables contains unsupported attributes")
    if any(node.tag != "AlgebraicVariable" for node in algebraic_node):
        raise ValueError("FMI-LS-DAE AlgebraicVariables contains unsupported child elements")

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

    algebraic_nodes = list(algebraic_node)
    if len(algebraic_nodes) > MAX_EQUATIONS or len(list(structure_node)) > MAX_EQUATIONS:
        raise ValueError("FMI-LS-DAE equation count exceeds the limit")
    algebraic_references = tuple(_value_reference(node, label="AlgebraicVariable") for node in algebraic_nodes)
    if len(set(algebraic_references)) != len(algebraic_references):
        raise ValueError("FMI-LS-DAE algebraic variable references must be unique")
    for node in algebraic_nodes:
        _validate_annotations_only(node, label="AlgebraicVariable", allowed_attributes={"valueReference"})
        if not variables_by_reference.get(_value_reference(node, label="AlgebraicVariable")):
            raise ValueError("FMI-LS-DAE AlgebraicVariable references an unknown model variable")

    model_structure = _parse_model_structure(
        structure_node,
        variables,
        variables_by_reference,
        set(algebraic_references),
    )
    declared_output_references = {
        entry.value_reference for entry in model_structure if entry.kind == "Output"
    }
    model_output_references = {
        int(getattr(variable, "valueReference"))
        for variable in variables
        if getattr(variable, "causality", None) == "output"
        and isinstance(getattr(variable, "valueReference", None), int)
        and not isinstance(getattr(variable, "valueReference", None), bool)
    }
    if declared_output_references != model_output_references:
        raise ValueError("FMI-LS-DAE ModelStructure must declare every model output exactly once")
    residual_references = tuple(
        entry.value_reference for entry in model_structure if entry.kind == "Residual"
    )
    derivative_references = tuple(
        entry.value_reference for entry in model_structure if entry.kind == "ContinuousStateDerivative"
    )
    event_indicator_references = tuple(
        entry.value_reference for entry in model_structure if entry.kind == "EventIndicator"
    )
    if len(derivative_references) != len(set(derivative_references)):
        raise ValueError("FMI-LS-DAE continuous state derivative references must be unique")
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
    derivative_names, derivative_types = resolve_real_variables(derivative_references, "continuous state derivatives")
    state_references: list[int] = []
    for derivative_reference in derivative_references:
        derivative_variable = next(
            (candidate for candidate in variables_by_reference[derivative_reference]
             if getattr(candidate, "derivative", None) is not None),
            None,
        )
        state = getattr(derivative_variable, "derivative", None)
        state_reference = getattr(state, "valueReference", state)
        if isinstance(state_reference, bool) or not isinstance(state_reference, int):
            raise ValueError("FMI-LS-DAE derivative does not identify its continuous state")
        if state_reference in state_references:
            raise ValueError("FMI-LS-DAE derivatives must identify unique continuous states")
        derivative_type = getattr(derivative_variable, "type", None)
        if not any(
            getattr(candidate, "type", None) == derivative_type
            and getattr(candidate, "variability", None) == "continuous"
            for candidate in variables_by_reference.get(state_reference, [])
        ):
            raise ValueError("FMI-LS-DAE derivative must identify a continuous state of the same type")
        state_references.append(state_reference)
    if set(algebraic_references).intersection(residual_references):
        raise ValueError("FMI-LS-DAE algebraic and residual variables must be distinct")
    residual_entries = tuple(entry for entry in model_structure if entry.kind == "Residual")
    for algebraic_reference in algebraic_references:
        if not any(
            entry.dependencies is None or algebraic_reference in entry.dependencies
            for entry in residual_entries
        ):
            raise ValueError(
                "FMI-LS-DAE algebraic variables must affect at least one declared residual"
            )

    variables_by_reference_first = {
        reference: candidates[0]
        for reference, candidates in variables_by_reference.items()
        if candidates
    }
    sizes_by_reference: dict[int, int] = {}
    for reference, variable in variables_by_reference_first.items():
        sizes_by_reference[reference] = _model_variable_size(variable, variables_by_reference_first)

    derivative_variable_by_reference = {
        reference: next(
            candidate for candidate in variables_by_reference[reference]
            if getattr(candidate, "type", None) in {"Float32", "Float64"}
            and getattr(candidate, "variability", None) == "continuous"
            and getattr(candidate, "derivative", None) is not None
        )
        for reference in derivative_references
    }
    derivative_count = sum(
        _model_variable_size(variable, variables_by_reference_first)
        for variable in derivative_variable_by_reference.values()
    )
    declared_state_count = getattr(model_description, "numberOfContinuousStates", None)
    if (
        isinstance(declared_state_count, int)
        and not isinstance(declared_state_count, bool)
        and derivative_count != declared_state_count
    ):
        raise ValueError(
            "FMI-LS-DAE ModelStructure must declare every continuous-state derivative in FMI order"
        )

    algebraic_count = sum(sizes_by_reference[reference] for reference in algebraic_references)
    residual_count = sum(sizes_by_reference[reference] for reference in residual_references)
    if algebraic_count != residual_count:
        raise ValueError("FMI-LS-DAE algebraic and residual arrays must contain the same number of values")

    event_indicator_count = sum(sizes_by_reference[reference] for reference in event_indicator_references)
    declared_event_indicator_count = getattr(model_description, "numberOfEventIndicators", None)
    if (
        isinstance(declared_event_indicator_count, int)
        and not isinstance(declared_event_indicator_count, bool)
        and event_indicator_count != declared_event_indicator_count
    ):
        raise ValueError("FMI-LS-DAE EventIndicator declarations do not match the FMI event indicator count")

    return FmiLsDaeDefinition(
        manifest_version=manifest_version,
        enable_dae_value_reference=enable_reference,
        enable_dae_variable_name=str(enable_variable.name),
        algebraic_value_references=algebraic_references,
        residual_value_references=residual_references,
        algebraic_names=algebraic_names,
        residual_names=residual_names,
        algebraic_types=algebraic_types,
        residual_types=residual_types,
        derivative_value_references=derivative_references,
        derivative_names=derivative_names,
        derivative_types=derivative_types,
        state_value_references=tuple(state_references),
        model_structure=model_structure,
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
        input_clock_references: list[int] | None = None,
        clock_schedules: list[Fmi3InputClockSchedule] | None = None,
        initial_input_clock_references: set[int] | None = None,
        initial_clocked_outputs: dict[str, Any] | None = None,
        initial_unknown_values: dict[int, Any] | None = None,
        restoring_state: bool = False,
    ):
        try:
            from sundials4py import core, idas
        except ImportError as exc:
            raise RuntimeError("FMI-LS-DAE execution requires the SUNDIALS IDAS runtime") from exc

        self._core = core
        self._idas = idas
        self._fmu = fmu
        self._model_description = model_description
        self._definition = definition
        self._initial_unknown_values = dict(initial_unknown_values or {})
        declared_initial_unknowns = set(definition.initial_unknown_value_references)
        if set(self._initial_unknown_values) - declared_initial_unknowns:
            raise ValueError("FMI-LS-DAE initialization data contains an undeclared InitialUnknown")
        self._input_clock_references = list(input_clock_references or [])
        self._clock_schedules = list(clock_schedules or [])
        self._initial_input_clock_references = set(initial_input_clock_references or [])
        self._next_event_time_defined = False
        self._next_event_time: float | None = None
        self._event_states_changed = False
        self._event_nominals_changed = False
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
        self._variables_by_reference = variables_by_reference
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
        self._derivative_variables = self._resolve_variable_specs(
            definition.derivative_value_references,
            definition.derivative_names,
            definition.derivative_types,
            variables_by_name,
            variables_by_reference,
        )
        state_variables = []
        for reference in definition.state_value_references:
            variable = variables_by_reference.get(reference)
            if variable is None:
                raise ValueError("FMI-LS-DAE continuous state is missing from the model description")
            state_variables.append(variable)
        self._state_variables = self._resolve_variable_specs(
            definition.state_value_references,
            tuple(str(variable.name) for variable in state_variables),
            tuple(str(variable.type) for variable in state_variables),
            variables_by_name,
            variables_by_reference,
        )
        if len(self._state_variables) != len(self._derivative_variables) or any(
            state.size != derivative.size
            for state, derivative in zip(self._state_variables, self._derivative_variables, strict=True)
        ):
            raise ValueError("FMI-LS-DAE state and derivative dimensions do not match")
        self._output_value_references = set(definition.output_value_references)
        self._clocked_state_variables = tuple(
            variables_by_reference[reference]
            for reference in definition.clocked_state_value_references
            if reference in variables_by_reference
        )
        if len(self._clocked_state_variables) != len(definition.clocked_state_value_references):
            raise ValueError("FMI-LS-DAE clocked state is missing from the model description")
        self._event_indicator_variables = tuple(
            variables_by_reference[reference]
            for reference in definition.event_indicator_value_references
            if reference in variables_by_reference
        )
        if len(self._event_indicator_variables) != len(definition.event_indicator_value_references):
            raise ValueError("FMI-LS-DAE event indicator is missing from the model description")
        get_event_indicator_count = getattr(fmu, "getNumberOfEventIndicators", None)
        raw_event_indicator_count = (
            get_event_indicator_count()
            if callable(get_event_indicator_count)
            else getattr(model_description, "numberOfEventIndicators", 0)
        )
        if (
            isinstance(raw_event_indicator_count, bool)
            or not isinstance(raw_event_indicator_count, int)
            or raw_event_indicator_count < 0
        ):
            raise ValueError("FMU returned an invalid number of event indicators")
        self._event_indicator_count = raw_event_indicator_count
        declared_event_indicator_count = sum(
            self._variable_size(variable, variables_by_reference)
            for variable in self._event_indicator_variables
        )
        if declared_event_indicator_count != self._event_indicator_count:
            raise ValueError(
                "FMI-LS-DAE EventIndicator declarations do not match the FMI event indicator count"
            )
        self._clocked_output_variables = tuple(
            variable
            for variable in (getattr(model_description, "modelVariables", []) or [])
            if getattr(variable, "causality", None) == "output"
            and getattr(variable, "type", None) != "Clock"
            and bool(getattr(variable, "clocks", None))
            and getattr(variable, "valueReference", None) in self._output_value_references
        )
        self._output_clock_variables = tuple(
            variable
            for variable in (getattr(model_description, "modelVariables", []) or [])
            if getattr(variable, "type", None) == "Clock"
            and getattr(variable, "causality", None) == "output"
            and getattr(variable, "valueReference", None) in self._output_value_references
        )
        self._pending_event_outputs: dict[str, Any] = {}
        self._clocked_output_cache: dict[str, Any] = dict(initial_clocked_outputs or {})
        self._state_count = int(fmu.getNumberOfContinuousStates())
        derivative_count = sum(spec.size for spec in self._derivative_variables)
        if derivative_count != self._state_count:
            raise ValueError(
                "FMI-LS-DAE ModelStructure derivative count does not match the FMU continuous-state count"
            )
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
            if not restoring_state:
                self._initialize_event_mode(float(start_time))
            self._initialize_solver(float(start_time), calculate_consistent_initial_conditions=not restoring_state)
        except Exception:
            self.close()
            raise

    def _initialize_solver(
        self,
        start_time: float,
        *,
        calculate_consistent_initial_conditions: bool = True,
    ) -> None:
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
        state_values = self._seed_initial_unknowns(self._state_variables, state_values)
        algebraic_values = self._seed_initial_unknowns(self._algebraic_variables, algebraic_values)
        derivative_values = self._seed_initial_unknowns(self._derivative_variables, derivative_values)
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
        if self._event_indicator_count:
            self._check_sundials(
                self._idas.IDARootInit(
                    self._ida_memory,
                    self._event_indicator_count,
                    self._event_indicators,
                ),
                "configure FMI event indicators",
            )
            self._check_sundials(
                self._idas.IDASetRootDirection(self._ida_memory, [0] * self._event_indicator_count),
                "configure FMI event indicator directions",
            )
            no_inactive_warning = getattr(self._idas, "IDASetNoInactiveRootWarn", None)
            if no_inactive_warning is not None:
                self._check_sundials(
                    no_inactive_warning(self._ida_memory),
                    "configure inactive event indicator handling",
                )
        if calculate_consistent_initial_conditions:
            self._calculate_consistent_initial_conditions(start_time, 1e-6)

    def _initialize_event_mode(self, start_time: float) -> None:
        """Resolve FMI 3 initialization events and clocks due at simulation start."""
        initial_clocks = set(due_fmi3_clock_references(self._clock_schedules, start_time))
        initial_clocks.update(self._initial_input_clock_references)
        terminated = self._resolve_event_mode(
            start_time,
            initial_clocks,
            defer_clock_activation=bool(initial_clocks),
        )
        if terminated:
            raise RuntimeError("FMI Model Exchange requested termination during DAE initialization")

    def _update_discrete_states(
        self,
        current_time: float,
        *,
        activated_clocks: set[int] | None = None,
        refresh_tunable_clocks: bool = False,
    ) -> bool:
        needs_update = True
        terminate = False
        states_changed = False
        nominals_changed = False
        next_event_time_defined = False
        next_event_time: float | None = None
        for _ in range(1000):
            (
                needs_update,
                terminate,
                iteration_nominals_changed,
                iteration_states_changed,
                next_event_time_defined,
                next_event_time,
            ) = self._fmu.updateDiscreteStates()
            states_changed = states_changed or bool(iteration_states_changed)
            nominals_changed = nominals_changed or bool(iteration_nominals_changed)
            if terminate or not needs_update:
                break
        else:
            raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")
        if next_event_time_defined:
            if (
                next_event_time is None
                or isinstance(next_event_time, bool)
                or not isinstance(next_event_time, (int, float))
                or not math.isfinite(float(next_event_time))
            ):
                raise RuntimeError("FMU returned an invalid next event time")
            validated_next_event_time = float(next_event_time)
            if validated_next_event_time < current_time - max(1e-12, abs(current_time) * 1e-12):
                raise RuntimeError("FMU returned a next event time before the current simulation time")
        else:
            validated_next_event_time = None
        self._next_event_time_defined = bool(next_event_time_defined)
        self._next_event_time = validated_next_event_time
        self._event_states_changed = states_changed
        self._event_nominals_changed = nominals_changed
        self._refresh_aperiodic_clocks(
            current_time,
            activated_clocks or set(),
            refresh_tunable_clocks=refresh_tunable_clocks,
        )
        return bool(terminate)

    def _set_input_clock_activations(self, active_references: set[int]) -> None:
        active = [reference for reference in self._input_clock_references if reference in active_references]
        if active:
            self._fmu.setClock(
                active,
                [True] * len(active),
            )

    def _process_event(
        self,
        current_time: float,
        active_clock_references: list[int],
        *,
        defer_clock_activation: bool = False,
    ) -> bool:
        self._fmu.enterEventMode()
        return self._resolve_event_mode(
            current_time,
            set(active_clock_references),
            defer_clock_activation=defer_clock_activation,
        )

    def _resolve_event_mode(
        self,
        current_time: float,
        active_clock_references: set[int],
        *,
        defer_clock_activation: bool = False,
        refresh_tunable_clocks: bool = False,
    ) -> bool:
        """Iterate discrete events, including zero-delay aperiodic clock ticks."""
        pending_clocks = set(active_clock_references)
        clock_activation_deferred = bool(defer_clock_activation and pending_clocks)
        for _ in range(1000):
            activated_clocks: set[int] = set()
            if pending_clocks and not clock_activation_deferred:
                activated_clocks = pending_clocks
                self._set_input_clock_activations(activated_clocks)
                consume_fmi3_clock_ticks(self._clock_schedules, activated_clocks, current_time)
                pending_clocks = set()
            terminated = self._update_discrete_states(
                current_time,
                activated_clocks=activated_clocks,
                refresh_tunable_clocks=refresh_tunable_clocks,
            )
            self._capture_event_outputs(activated_clocks)
            if terminated:
                return True
            # FMI requires the FMU to deactivate any input Clock activated for
            # updateDiscreteStates. Re-activate only when an aperiodic Clock
            # reports another tick at this same simulation time. An FMU may
            # also announce another super-dense time event at this instant.
            due_clocks = set(due_fmi3_clock_references(self._clock_schedules, current_time))
            tolerance = max(1e-12, abs(current_time) * 1e-12)
            time_event_due = (
                self._next_event_time_defined
                and self._next_event_time is not None
                and self._next_event_time <= current_time + tolerance
            )
            if clock_activation_deferred:
                if time_event_due:
                    continue
                clock_activation_deferred = False
                continue
            pending_clocks = due_clocks
            if not pending_clocks and not time_event_due:
                break
        else:
            raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")
        self._fmu.enterContinuousTimeMode()
        return False

    def _refresh_aperiodic_clocks(
        self,
        current_time: float,
        activated_clocks: set[int],
        *,
        refresh_tunable_clocks: bool = False,
    ) -> None:
        refresh_fmi3_clock_schedules(
            self._fmu,
            self._clock_schedules,
            current_time,
            activated_clocks,
            refresh_tunable_clocks=refresh_tunable_clocks,
        )

    def _next_time_event(self) -> float | None:
        event_times = [
            schedule.next_time for schedule in self._clock_schedules
            if schedule.next_time is not None
        ]
        if self._next_event_time_defined and self._next_event_time is not None:
            event_times.append(self._next_event_time)
        return min(event_times) if event_times else None

    def _restart_solver(self, current_time: float, step_size: float) -> None:
        state_values = self._read_continuous_states()
        algebraic_values = self._read_algebraic_values()
        derivative_values = self._read_continuous_state_derivatives()
        self._write_vector(self._state_vector, [*state_values, *algebraic_values])
        self._write_vector(self._derivative_vector, [*derivative_values, *([0.0] * self._algebraic_count)])
        self._check_sundials(
            self._idas.IDAReInit(self._ida_memory, float(current_time), self._state_vector, self._derivative_vector),
            "restart SUNDIALS after an FMI event",
        )
        self._calculate_consistent_initial_conditions(
            current_time,
            max(1e-8, min(abs(step_size) * 0.01, 1e-3)),
        )

    def _event_indicators(
        self,
        time: float,
        state_vector: Any,
        _derivative_vector: Any,
        indicator_values: Any,
        _user_data: Any,
    ) -> int:
        try:
            values = self._core.N_VGetNumpyArray(state_vector)
            self._set_fmu_for_solver_point(float(time), values)
            fmu_indicators = (ctypes.c_double * self._event_indicator_count)()
            self._fmu.getEventIndicators(fmu_indicators, self._event_indicator_count)
            indicator_values[:] = [float(value) for value in fmu_indicators]
            if not all(math.isfinite(float(value)) for value in indicator_values):
                raise RuntimeError("FMU returned a non-finite event indicator")
            return 0
        except Exception as exc:
            self._callback_error = exc
            return -1

    @property
    def state_count(self) -> int:
        return self._state_count

    def advance(self, current_time: float, target_time: float, *, step_size: float) -> dict[str, Any]:
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
        tolerance = max(1e-12, abs(current_time) * 1e-12)
        due_clocks = due_fmi3_clock_references(self._clock_schedules, current_time)
        model_time_event_due = (
            self._next_event_time_defined
            and self._next_event_time is not None
            and self._next_event_time <= current_time + tolerance
        )
        next_event_time = self._next_time_event()
        if next_event_time is not None and next_event_time <= current_time + tolerance:
            terminated = self._process_event(
                current_time,
                due_clocks,
                defer_clock_activation=model_time_event_due,
            )
            if not terminated:
                self._restart_solver(current_time, step_size)
            return {
                "time": current_time,
                "eventOccurred": True,
                "terminateSimulation": terminated,
                "earlyReturn": True,
            }

        integration_target = target_time
        time_event_due = next_event_time is not None and next_event_time <= target_time + tolerance
        if time_event_due and next_event_time is not None:
            integration_target = min(target_time, next_event_time)
        self._check_sundials(
            self._idas.IDASetStopTime(self._ida_memory, integration_target),
            "set SUNDIALS integration stop time",
        )
        status, reached_time = self._idas.IDASolve(
            self._ida_memory,
            float(integration_target),
            self._output_state_vector,
            self._output_derivative_vector,
            self.IDA_NORMAL,
        )
        self._check_callback_and_sundials(status, "integrate FMI-LS-DAE model")
        reached_time = float(reached_time)
        if not math.isfinite(reached_time) or reached_time < current_time - 1e-10 or reached_time > integration_target + 1e-10:
            raise RuntimeError("SUNDIALS returned a time outside the requested DAE interval")
        self._copy_vector(self._output_state_vector, self._state_vector)
        self._copy_vector(self._output_derivative_vector, self._derivative_vector)
        self._sync_fmu(reached_time, self._state_vector)
        enter_event_mode = False
        terminate_simulation = False
        needs_completed_step = bool(
            getattr(getattr(self._model_description, "modelExchange", None), "needsCompletedIntegratorStep", False)
        )
        if needs_completed_step:
            enter_event_mode, terminate_simulation = self._fmu.completedIntegratorStep(
                noSetFMUStatePriorToCurrentPoint=False,
            )
        if terminate_simulation:
            return {
                "time": reached_time,
                "eventOccurred": False,
                "terminateSimulation": True,
                "earlyReturn": reached_time < target_time - tolerance,
            }

        root_event = status == getattr(self._idas, "IDA_ROOT_RETURN", 2)
        reached_model_time_event = bool(
            self._next_event_time_defined
            and self._next_event_time is not None
            and abs(reached_time - self._next_event_time) <= max(tolerance, 1e-10)
        )
        scheduled_clocks = due_fmi3_clock_references(self._clock_schedules, reached_time)
        clock_event_reached = bool(scheduled_clocks)
        event_occurred = bool(root_event or reached_model_time_event or clock_event_reached or enter_event_mode)
        if event_occurred:
            terminate_simulation = self._process_event(
                reached_time,
                scheduled_clocks,
                defer_clock_activation=bool(root_event or reached_model_time_event or enter_event_mode),
            )
            if not terminate_simulation:
                self._restart_solver(reached_time, step_size)
        return {
            "time": reached_time,
            "eventOccurred": event_occurred,
            "terminateSimulation": bool(terminate_simulation),
            "earlyReturn": reached_time < target_time - tolerance,
        }

    def apply_event_mode(
        self,
        current_time: float,
        apply_values: Any,
        *,
        step_size: float,
        active_clock_references: list[int] | None = None,
        refresh_tunable_clocks: bool = False,
    ) -> bool:
        """Apply discrete inputs or tunable parameters at an FMI event instant."""
        if self._closed:
            raise RuntimeError("FMI-LS-DAE solver is closed")
        self._fmu.enterEventMode()
        apply_values()
        active_clocks = set(active_clock_references or [])
        active_clocks.update(due_fmi3_clock_references(self._clock_schedules, current_time))
        terminated = self._resolve_event_mode(
            current_time,
            active_clocks,
            refresh_tunable_clocks=refresh_tunable_clocks,
        )
        if not terminated:
            self._restart_solver(current_time, step_size)
        return terminated

    def take_event_outputs(self) -> dict[str, Any]:
        outputs = self._pending_event_outputs
        self._pending_event_outputs = {}
        return outputs

    def clocked_output_cache(self) -> dict[str, Any]:
        return dict(self._clocked_output_cache)

    def _capture_event_outputs(self, active_input_clocks: set[int]) -> None:
        clock_variables = {
            int(getattr(variable, "valueReference")): variable
            for variable in (getattr(self._model_description, "modelVariables", []) or [])
            if getattr(variable, "type", None) == "Clock"
            and isinstance(getattr(variable, "valueReference", None), int)
        }
        required_clock_references = {
            int(getattr(clock, "valueReference", clock))
            for variable in self._clocked_output_variables
            for clock in (getattr(variable, "clocks", []) or [])
            if isinstance(getattr(clock, "valueReference", clock), int)
            and not isinstance(getattr(clock, "valueReference", clock), bool)
        }
        required_clock_references.update(
            int(getattr(clock, "valueReference", clock))
            for variable in self._clocked_state_variables
            for clock in (getattr(variable, "clocks", []) or [])
            if isinstance(getattr(clock, "valueReference", clock), int)
            and not isinstance(getattr(clock, "valueReference", clock), bool)
        )
        required_clock_references.update(
            int(variable.valueReference) for variable in self._output_clock_variables
        )
        query_references = [
            reference for reference in required_clock_references
            if reference in clock_variables
            and getattr(clock_variables[reference], "causality", None) in {"output", "local"}
        ]
        active_clocks = set(active_input_clocks)
        clock_values: dict[int, bool] = {}
        if query_references:
            for reference, value in zip(query_references, self._fmu.getClock(query_references), strict=True):
                active = bool(value)
                clock_values[reference] = active
                variable = clock_variables[reference]
                if getattr(variable, "causality", None) == "output":
                    name = str(variable.name)
                    self._pending_event_outputs[name] = bool(
                        self._pending_event_outputs.get(name, False) or active
                    )

        for variable in self._clocked_output_variables:
            associated_references = {
                int(getattr(clock, "valueReference", clock))
                for clock in (getattr(variable, "clocks", []) or [])
                if isinstance(getattr(clock, "valueReference", clock), int)
                and not isinstance(getattr(clock, "valueReference", clock), bool)
            }
            if not associated_references.intersection(active_clocks | {
                reference for reference, active in clock_values.items() if active
            }):
                continue
            value_reference = int(variable.valueReference)
            value_type = str(getattr(variable, "type", ""))
            if value_type == "Enumeration":
                value_type = "Int64"
            getter = getattr(self._fmu, f"get{value_type}", None)
            if getter is None:
                raise RuntimeError(f"FMI clocked output '{variable.name}' has an unsupported type")
            size = self._variable_size(variable, self._variables_by_reference)
            is_array = self._is_array(variable)
            values = getter([value_reference], nValues=size) if is_array else getter([value_reference])
            normalized = [self._normalise_clocked_output(value_type, value) for value in values]
            normalized_value = normalized if is_array else normalized[0]
            self._clocked_output_cache[str(variable.name)] = normalized_value
            self._pending_event_outputs[str(variable.name)] = normalized_value

    @staticmethod
    def _normalise_clocked_output(value_type: str, value: Any) -> Any:
        if value_type in {"Float32", "Float64", "Real"}:
            number = float(value)
            if not math.isfinite(number):
                raise RuntimeError("FMU returned a non-finite clocked output")
            return number
        if value_type in {"Int8", "UInt8", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64", "Enumeration"}:
            return int(value)
        if value_type in {"Boolean", "Clock"}:
            return bool(value)
        if value_type == "String":
            return str(value)
        if value_type == "Binary":
            import base64
            return base64.b64encode(bytes(value)).decode("ascii")
        return value

    def snapshot_event_schedule(self) -> dict[str, Any]:
        return {
            "nextEventTimeDefined": self._next_event_time_defined,
            **({"nextEventTime": self._next_event_time} if self._next_event_time_defined else {}),
            "clockSchedules": [
                {
                    "valueReference": schedule.value_reference,
                    "intervalVariability": schedule.variability,
                    "interval": schedule.interval,
                    "shift": schedule.shift,
                    "nextTime": schedule.next_time,
                    "lastTickTime": schedule.last_tick_time,
                    "hasTicked": schedule.has_ticked,
                }
                for schedule in self._clock_schedules
            ],
            "clockedOutputCache": dict(self._clocked_output_cache),
        }

    def restore_event_schedule(self, metadata: dict[str, Any], current_time: float) -> None:
        if type(metadata.get("nextEventTimeDefined")) is not bool:
            raise ValueError("FMI state checkpoint has invalid DAE event metadata")
        next_defined = metadata["nextEventTimeDefined"]
        next_time = metadata.get("nextEventTime")
        if next_defined:
            if (
                next_time is None
                or isinstance(next_time, bool)
                or not isinstance(next_time, (int, float))
                or not math.isfinite(float(next_time))
            ):
                raise ValueError("FMI state checkpoint has invalid DAE event time")
            validated_next_time = float(next_time)
            if validated_next_time < current_time - max(1e-12, abs(current_time) * 1e-12):
                raise ValueError("FMI state checkpoint has a DAE event before its simulation time")
        else:
            validated_next_time = None
        raw_schedules = metadata.get("clockSchedules")
        if not isinstance(raw_schedules, list) or len(raw_schedules) != len(self._clock_schedules):
            raise ValueError("FMI state checkpoint has invalid DAE clock metadata")
        raw_clocked_outputs = metadata.get("clockedOutputCache")
        if not isinstance(raw_clocked_outputs, dict):
            raise ValueError("FMI state checkpoint has invalid DAE clocked output metadata")
        clocked_variables = {str(variable.name): variable for variable in self._clocked_output_variables}
        normalized_clocked_outputs: dict[str, Any] = {}
        for name, value in raw_clocked_outputs.items():
            variable = clocked_variables.get(name) if isinstance(name, str) else None
            if variable is None:
                raise ValueError("FMI state checkpoint has an unknown DAE clocked output")
            value_type = str(getattr(variable, "type", ""))
            if value_type == "Enumeration":
                value_type = "Int64"
            size = self._variable_size(variable, self._variables_by_reference)
            is_array = self._is_array(variable)
            if is_array and not isinstance(value, list):
                raise ValueError("FMU state checkpoint has an invalid DAE clocked output array")
            raw_values = value if is_array else [value]
            if len(raw_values) != size:
                raise ValueError("FMI state checkpoint has an invalid DAE clocked output size")
            try:
                normalized = [self._normalise_clocked_output(value_type, item) for item in raw_values]
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("FMI state checkpoint has an invalid DAE clocked output value") from exc
            normalized_clocked_outputs[name] = normalized if is_array else normalized[0]
        by_reference = {schedule.value_reference: schedule for schedule in self._clock_schedules}
        restored: set[int] = set()
        for item in raw_schedules:
            if not isinstance(item, dict):
                raise ValueError("FMI state checkpoint has invalid DAE clock metadata")
            reference = item.get("valueReference")
            normalized_reference = (
                reference
                if isinstance(reference, int) and not isinstance(reference, bool)
                else None
            )
            if normalized_reference is None:
                raise ValueError("FMI state checkpoint has invalid DAE clock reference")
            schedule = by_reference.get(normalized_reference)
            if schedule is None or normalized_reference in restored:
                raise ValueError("FMI state checkpoint has invalid DAE clock reference")
            if item.get("intervalVariability") != schedule.variability:
                raise ValueError("FMI state checkpoint DAE clock configuration does not match the FMU")
            interval = _checkpoint_optional_finite_number(item.get("interval"), "clock interval")
            next_clock_time = _checkpoint_optional_finite_number(item.get("nextTime"), "clock next time")
            last_tick = _checkpoint_finite_number(item.get("lastTickTime"), "clock last tick")
            shift = _checkpoint_finite_number(item.get("shift"), "clock shift")
            has_ticked = item.get("hasTicked")
            if type(has_ticked) is not bool:
                raise ValueError("FMI state checkpoint has invalid DAE clock tick metadata")
            if next_clock_time is not None and next_clock_time < current_time - max(1e-12, abs(current_time) * 1e-12):
                raise ValueError("FMI state checkpoint has a DAE clock before its simulation time")
            if last_tick > current_time + max(1e-12, abs(current_time) * 1e-12):
                raise ValueError("FMI state checkpoint has a DAE clock tick after its simulation time")
            schedule.interval = interval
            schedule.next_time = next_clock_time
            schedule.last_tick_time = last_tick
            schedule.shift = shift
            schedule.has_ticked = has_ticked
            restored.add(normalized_reference)
        self._next_event_time_defined = next_defined
        self._next_event_time = validated_next_time
        self._clocked_output_cache = normalized_clocked_outputs

    def restart_after_input_change(self, current_time: float, *, step_size: float) -> None:
        """Discard IDAS history after an input update and restore DAE consistency."""
        if self._closed:
            raise RuntimeError("FMI-LS-DAE solver is closed")
        self._restart_solver(current_time, step_size)

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

    def _seed_initial_unknowns(
        self,
        variables: tuple[_RealVariableSpec, ...],
        values: list[float],
    ) -> list[float]:
        """Use the manifest's ordered InitialUnknown values as solver guesses."""
        seeded = list(values)
        for variable in variables:
            if variable.value_reference not in self._initial_unknown_values:
                continue
            raw = self._initial_unknown_values[variable.value_reference]
            raw_values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
            if len(raw_values) != variable.size:
                raise ValueError(
                    f"FMI-LS-DAE InitialUnknown {variable.value_reference} has an unexpected value count"
                )
            try:
                normalized = [float(value) for value in raw_values]
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    f"FMI-LS-DAE InitialUnknown {variable.value_reference} is not numeric"
                ) from exc
            if not all(math.isfinite(value) for value in normalized):
                raise ValueError(
                    f"FMI-LS-DAE InitialUnknown {variable.value_reference} is not finite"
                )
            seeded[variable.offset:variable.offset + variable.size] = normalized
        return seeded

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
        return _model_variable_size(variable, variables_by_reference)

    @staticmethod
    def _is_array(variable: Any) -> bool:
        shape = getattr(variable, "shape", None)
        dimensions = getattr(variable, "dimensions", None)
        return bool(
            (isinstance(shape, (list, tuple)) and shape)
            or (isinstance(dimensions, (list, tuple)) and dimensions)
        )

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


def _checkpoint_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"FMI state checkpoint has an invalid DAE {label}")
    return float(value)


def _checkpoint_optional_finite_number(value: Any, label: str) -> float | None:
    return None if value is None else _checkpoint_finite_number(value, label)
