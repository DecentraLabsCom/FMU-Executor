"""Normalize parsed FMI model descriptions for the Executor contract."""

from __future__ import annotations

import base64
import math
from typing import Any


def describe_model_description(
    md: Any,
    *,
    public_only: bool = False,
    causality: str | None = None,
    display_unit_inverses: dict[tuple[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Normalize a parsed FMI description without reopening the FMU archive."""

    def finite_number(value: Any, *, integer: bool = False) -> int | float | None:
        if value is None:
            return None
        try:
            parsed = int(value) if integer else float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if isinstance(parsed, float) and not math.isfinite(parsed):
            return None
        return parsed

    def display_unit_for(var: Any, declared_type: Any) -> dict[str, Any] | None:
        unit_name = getattr(var, "unit", None) or getattr(declared_type, "unit", None)
        display_name = getattr(var, "displayUnit", None) or getattr(declared_type, "displayUnit", None)
        if not isinstance(unit_name, str) or not unit_name or not isinstance(display_name, str) or not display_name:
            return None

        unit = next(
            (item for item in (getattr(md, "unitDefinitions", None) or []) if getattr(item, "name", None) == unit_name),
            None,
        )
        display = next(
            (item for item in (getattr(unit, "displayUnits", None) or []) if getattr(item, "name", None) == display_name),
            None,
        )
        if display is None:
            return None

        try:
            factor = float(getattr(display, "factor", 1.0))
            offset = float(getattr(display, "offset", 0.0))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(factor) or factor == 0 or not math.isfinite(offset):
            return None

        fmi_major_version = str(getattr(md, "fmiVersion", "")).split(".", 1)[0]
        raw_inverse = getattr(display, "inverse", None)
        if raw_inverse is None:
            if fmi_major_version == "3" and display_unit_inverses is None:
                return None
            inverse = bool(display_unit_inverses and display_unit_inverses.get((unit_name, display_name), False))
        else:
            inverse = raw_inverse is True or str(raw_inverse).strip().lower() in {"true", "1"}
        if inverse and (fmi_major_version == "1" or offset != 0):
            return None

        # The public metadata uses one consistent equation for every FMI
        # version: display = factor * model_value + offset.
        if str(getattr(md, "fmiVersion", "")).split(".", 1)[0] == "1":
            factor, offset = 1.0 / factor, -offset / factor
        variable_relative = getattr(var, "relativeQuantity", None)
        type_relative = getattr(declared_type, "relativeQuantity", None)
        if (
            variable_relative is True
            or type_relative is True
            or str(variable_relative).strip().lower() in {"true", "1"}
            or str(type_relative).strip().lower() in {"true", "1"}
        ):
            offset = 0.0
        result: dict[str, Any] = {"name": display_name, "factor": factor, "offset": offset}
        if inverse:
            result["inverse"] = True
        return result

    integer_types = {
        "Integer", "Enumeration", "Int8", "UInt8", "Int16", "UInt16",
        "Int32", "UInt32", "Int64", "UInt64",
    }

    variables: list[dict[str, Any]] = []
    for var in md.modelVariables:
        # Local variables are internal state, not part of the FMI interface.
        # FMI's independent variable (usually time) and future causality values
        # remain available to callers of the reduced description.
        if public_only and var.causality == "local":
            continue
        if causality is not None and var.causality != causality:
            continue
        declared_type = getattr(var, "declaredType", None)
        entry: dict[str, Any] = {
            "name": var.name,
            "valueReference": var.valueReference,
            "causality": var.causality,
            "variability": var.variability,
        }
        if var.type:
            entry["type"] = var.type
        dimensions = getattr(var, "dimensions", None)
        if isinstance(dimensions, (list, tuple)) and dimensions:
            dimension_entries = []
            for dimension in dimensions:
                dimension_entry: dict[str, Any] = {}
                start = getattr(dimension, "start", None)
                if start is not None:
                    dimension_entry["start"] = start
                value_reference = getattr(dimension, "valueReference", None)
                if value_reference is not None:
                    dimension_entry["valueReference"] = int(value_reference)
                dimension_variable = getattr(dimension, "variable", None)
                dimension_variable_name = getattr(dimension_variable, "name", None)
                if isinstance(dimension_variable_name, str) and dimension_variable_name:
                    dimension_entry["variableName"] = dimension_variable_name[:256]
                if dimension_entry:
                    dimension_entries.append(dimension_entry)
            if dimension_entries:
                entry["dimensions"] = dimension_entries
        if var.start is not None:
            if var.type == "Binary" and isinstance(var.start, (bytes, bytearray)):
                entry["start"] = base64.b64encode(bytes(var.start)).decode("ascii")
            else:
                entry["start"] = var.start
        initial = getattr(var, "initial", None)
        if initial is not None:
            entry["initial"] = str(initial)
        nominal = finite_number(
            getattr(var, "nominal", None),
            integer=var.type in integer_types,
        )
        if nominal is not None:
            entry["nominal"] = nominal
        unit = getattr(var, "unit", None) or getattr(declared_type, "unit", None)
        if unit:
            entry["unit"] = unit
        quantity = getattr(var, "quantity", None) or getattr(declared_type, "quantity", None)
        if quantity:
            entry["quantity"] = str(quantity)[:256]
        display_name = getattr(var, "displayName", None)
        if isinstance(display_name, str) and display_name.strip():
            entry["displayName"] = display_name.strip()[:256]
        description = getattr(var, "description", None)
        if isinstance(description, str) and description.strip():
            entry["description"] = description.strip()[:4096]
        minimum = getattr(var, "min", None)
        if minimum is None:
            minimum = getattr(declared_type, "min", None)
        maximum = getattr(var, "max", None)
        if maximum is None:
            maximum = getattr(declared_type, "max", None)
        normalized_min = finite_number(minimum, integer=var.type in integer_types)
        normalized_max = finite_number(maximum, integer=var.type in integer_types)
        if normalized_min is not None:
            entry["min"] = normalized_min
        if normalized_max is not None:
            entry["max"] = normalized_max
        display_unit = display_unit_for(var, declared_type)
        if display_unit is not None:
            entry["displayUnit"] = display_unit
        if var.type == "Enumeration" and declared_type is not None:
            items = []
            for item in getattr(declared_type, "items", []) or []:
                value = finite_number(getattr(item, "value", None), integer=True)
                name = getattr(item, "name", None)
                if value is None or not isinstance(name, str) or not name.strip():
                    continue
                option = {"value": value, "name": name.strip()[:256]}
                item_description = getattr(item, "description", None)
                if isinstance(item_description, str) and item_description.strip():
                    option["description"] = item_description.strip()[:1024]
                items.append(option)
            if items and len(items) <= 256:
                entry["enumItems"] = items
        if declared_type is not None:
            declared_type_entry: dict[str, Any] = {}
            for field in ("name", "type", "description", "unit", "quantity", "displayUnit"):
                value = getattr(declared_type, field, None)
                if value is not None and value != "":
                    declared_type_entry[field] = str(value)[:4096]
            declared_minimum = getattr(declared_type, "min", None)
            declared_maximum = getattr(declared_type, "max", None)
            if declared_minimum is not None:
                declared_type_entry["min"] = finite_number(
                    declared_minimum,
                    integer=var.type in integer_types,
                )
            if declared_maximum is not None:
                declared_type_entry["max"] = finite_number(
                    declared_maximum,
                    integer=var.type in integer_types,
                )
            declared_items = []
            for item in getattr(declared_type, "items", []) or []:
                value = finite_number(getattr(item, "value", None), integer=True)
                name = getattr(item, "name", None)
                if value is None or not isinstance(name, str) or not name.strip():
                    continue
                item_entry = {"value": value, "name": name.strip()[:256]}
                item_description = getattr(item, "description", None)
                if isinstance(item_description, str) and item_description.strip():
                    item_entry["description"] = item_description.strip()[:1024]
                declared_items.append(item_entry)
            if declared_items and len(declared_items) <= 256:
                declared_type_entry["items"] = declared_items
            if declared_type_entry:
                entry["declaredType"] = declared_type_entry
        variables.append(entry)

    cs = md.coSimulation
    me = md.modelExchange

    result: dict[str, Any] = {
        "modelName": md.modelName,
        "guid": md.guid,
        "fmiVersion": md.fmiVersion,
        "supportsCoSimulation": cs is not None,
        "supportsModelExchange": me is not None,
        "modelVariables": variables,
    }

    for field, maximum_length in (
        ("description", 4096),
        ("author", 256),
        ("version", 128),
        ("license", 256),
        ("generationTool", 256),
    ):
        value = getattr(md, field, None)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()[:maximum_length]

    capabilities: dict[str, Any] = {}
    interface = cs or me
    if interface is not None:
        for attribute in (
            "canGetAndSetFMUstate",
            "canGetAndSetFMUState",
            "canSerializeFMUstate",
            "canSerializeFMUState",
            "canHandleVariableCommunicationStepSize",
            "providesDirectionalDerivative",
            "providesAdjointDerivatives",
        ):
            value = getattr(interface, attribute, None)
            if value is not None:
                capabilities[attribute] = bool(value)
        fixed_step = finite_number(getattr(interface, "fixedInternalStepSize", None))
        if fixed_step is not None:
            capabilities["fixedInternalStepSize"] = fixed_step
    if capabilities:
        result["capabilities"] = capabilities

    if cs is not None:
        result["fmiType"] = "CoSimulation"
        result["simulationKind"] = "coSimulation"
        result["simulationType"] = "CoSimulation"
    elif me is not None:
        result["fmiType"] = "ModelExchange"
        result["simulationKind"] = "modelExchange"
        result["simulationType"] = "ModelExchange"

    exp = md.defaultExperiment
    if exp:
        if exp.startTime is not None:
            result["defaultStartTime"] = float(exp.startTime)
        if exp.stopTime is not None:
            result["defaultStopTime"] = float(exp.stopTime)
        if exp.stepSize is not None:
            result["defaultStepSize"] = float(exp.stepSize)
        if exp.tolerance is not None:
            result["defaultTolerance"] = float(exp.tolerance)

    return result
