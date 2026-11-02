"""Time-based FMI 3 input-clock scheduling for FMI importers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


_PERIODIC_VARIABILITIES = frozenset({"constant", "fixed", "tunable"})
_APERIODIC_VARIABILITIES = frozenset({"changing", "countdown"})
_SCHEDULED_VARIABILITIES = _PERIODIC_VARIABILITIES | _APERIODIC_VARIABILITIES
FMI3_INTERVAL_NOT_YET_KNOWN = 0
FMI3_INTERVAL_UNCHANGED = 1
FMI3_INTERVAL_CHANGED = 2


@dataclass
class Fmi3InputClockSchedule:
    value_reference: int
    variability: str
    interval: float | None
    shift: float
    next_time: float | None
    last_tick_time: float
    has_ticked: bool = False
    interval_from_fmu: bool = False
    supports_fraction: bool = False


def read_fmi3_input_clock_schedules(
    fmu: Any,
    model_description: Any,
    start_time: float,
) -> tuple[list[int], list[Fmi3InputClockSchedule]]:
    """Read clock schedules while the FMU is in Initialization Mode."""
    if not math.isfinite(start_time):
        raise ValueError("FMI 3 clock schedule requires a finite start time")

    input_clock_references: list[int] = []
    schedules: list[Fmi3InputClockSchedule] = []
    for variable in getattr(model_description, "modelVariables", []) or []:
        if getattr(variable, "type", None) != "Clock" or getattr(variable, "causality", None) != "input":
            continue
        reference = getattr(variable, "valueReference", None)
        if isinstance(reference, bool) or not isinstance(reference, int) or reference < 0:
            raise ValueError("FMI 3 input Clock has an invalid valueReference")
        input_clock_references.append(reference)
        variability = getattr(variable, "intervalVariability", None)
        declared_type = getattr(variable, "declaredType", None)
        if variability is None and declared_type is not None:
            variability = getattr(declared_type, "intervalVariability", None)
        if variability not in _SCHEDULED_VARIABILITIES:
            # Triggered clocks are activated by explicit clock inputs, while
            # clocks with other semantics are not time-based importer tasks.
            continue

        if variability in _APERIODIC_VARIABILITIES and any(
            _clock_attribute(variable, attribute) is not None
            for attribute in ("intervalDecimal", "intervalCounter")
        ):
            raise ValueError(f"FMI 3 aperiodic input Clock {reference} must obtain its interval from the FMU")

        supports_fraction = bool(_clock_attribute(variable, "supportsFraction"))
        # FMI 3's fixed/tunable rules key off intervalDecimal: when it is
        # absent the importer obtains both interval and shift from the FMU.
        interval_is_declared = _clock_attribute(variable, "intervalDecimal") is not None
        if variability == "constant":
            # A constant periodic Clock declares its interval and shift in the
            # model description; there is no importer-selected interval.
            if not interval_is_declared:
                raise ValueError(f"FMI 3 constant periodic input Clock {reference} must declare intervalDecimal")
            interval = _read_declared_interval(variable, reference)
            interval_from_fmu = False
        elif variability in {"fixed", "tunable"}:
            interval = (
                _read_declared_interval(variable, reference)
                if interval_is_declared
                else _read_interval_from_fmu(fmu, reference, supports_fraction=supports_fraction)
            )
            if interval is None:
                raise ValueError(f"FMI 3 periodic input Clock {reference} has no initial interval")
            # FMI 3 requires the importer to call SetInterval for every fixed
            # and tunable periodic Clock. If the FMU supplied the initial
            # interval because intervalDecimal was omitted, pass that value
            # back to the FMU as the negotiated interval.
            _set_interval(fmu, reference, interval)
            interval_from_fmu = variability == "tunable" and not interval_is_declared
        else:
            interval = _read_interval_from_fmu(fmu, reference, supports_fraction=supports_fraction)
            interval_from_fmu = True
        if variability in _PERIODIC_VARIABILITIES:
            if interval is None or not math.isfinite(interval) or interval <= 0:
                raise ValueError(f"FMI 3 periodic input Clock {reference} has no positive interval")
            shift = (
                _read_shift(variable, reference)
                if variability == "constant" or interval_is_declared
                else _read_shift_from_fmu(fmu, reference, supports_fraction=supports_fraction)
            )
            if shift < 0:
                raise ValueError(f"FMI 3 periodic input Clock {reference} has a negative shift")
            next_time = start_time + shift
        else:
            shift = 0.0
            if interval is not None and (not math.isfinite(interval) or interval < 0):
                raise ValueError(f"FMI 3 aperiodic input Clock {reference} has an invalid interval")
            next_time = start_time + interval if interval is not None else None

        schedules.append(Fmi3InputClockSchedule(
            value_reference=reference,
            variability=str(variability),
            interval=interval,
            shift=shift,
            next_time=next_time,
            last_tick_time=start_time,
            interval_from_fmu=interval_from_fmu,
            supports_fraction=supports_fraction,
        ))

    if len(input_clock_references) != len(set(input_clock_references)):
        raise ValueError("FMI 3 input Clock valueReferences must be unique")
    return input_clock_references, schedules


def due_fmi3_clock_references(
    schedules: list[Fmi3InputClockSchedule],
    time: float,
) -> list[int]:
    tolerance = max(1e-12, abs(time) * 1e-12)
    return [
        schedule.value_reference
        for schedule in schedules
        if schedule.next_time is not None and schedule.next_time <= time + tolerance
    ]


def read_fmi3_clock_interval_update(
    fmu: Any,
    reference: int,
    *,
    supports_fraction: bool,
) -> tuple[int, float | None]:
    """Read an FMI clock interval and preserve its change qualifier."""
    if supports_fraction and hasattr(fmu, "getIntervalFraction"):
        from fmpy.fmi3 import fmi3IntervalQualifier, fmi3UInt64, fmi3ValueReference

        references = (fmi3ValueReference * 1)(reference)
        counters = (fmi3UInt64 * 1)()
        resolutions = (fmi3UInt64 * 1)()
        qualifiers = (fmi3IntervalQualifier * 1)()
        fmu.getIntervalFraction(references, counters, resolutions, qualifiers)
        qualifier = _validate_interval_qualifier(qualifiers[0], reference)
        if qualifier != FMI3_INTERVAL_CHANGED:
            return qualifier, None
        if int(resolutions[0]) <= 0:
            raise ValueError(f"FMI 3 input Clock {reference} returned an invalid fractional interval")
        return qualifier, int(counters[0]) / int(resolutions[0])
    if not hasattr(fmu, "getIntervalDecimal"):
        raise ValueError(f"FMI 3 input Clock {reference} has no interval getter")
    from fmpy.fmi3 import fmi3Float64, fmi3IntervalQualifier, fmi3ValueReference

    references = (fmi3ValueReference * 1)(reference)
    intervals = (fmi3Float64 * 1)()
    qualifiers = (fmi3IntervalQualifier * 1)()
    fmu.getIntervalDecimal(references, intervals, qualifiers)
    qualifier = _validate_interval_qualifier(qualifiers[0], reference)
    if qualifier != FMI3_INTERVAL_CHANGED:
        return qualifier, None
    return qualifier, _finite_number(intervals[0], f"FMI 3 input Clock {reference} interval")


def consume_fmi3_clock_ticks(
    schedules: list[Fmi3InputClockSchedule],
    references: set[int],
    time: float,
) -> None:
    """Advance periodic clock deadlines after an activation."""
    tolerance = max(1e-12, abs(time) * 1e-12)
    for schedule in schedules:
        if schedule.value_reference not in references:
            continue
        schedule.last_tick_time = time
        schedule.has_ticked = True
        if schedule.variability in _PERIODIC_VARIABILITIES:
            if schedule.interval is None or schedule.interval <= 0:
                raise RuntimeError("FMI 3 periodic input Clock lost its interval")
            schedule.next_time = time + schedule.interval
        elif schedule.interval is not None:
            schedule.next_time = time + schedule.interval
        else:
            schedule.next_time = None
        if schedule.next_time is not None and schedule.next_time < time - tolerance:
            raise RuntimeError("FMI 3 input Clock schedule moved backwards")


def refresh_fmi3_clock_schedules(
    fmu: Any,
    schedules: list[Fmi3InputClockSchedule],
    current_time: float,
    activated_clocks: set[int],
    *,
    refresh_tunable_clocks: bool = False,
) -> None:
    """Refresh FMU-provided intervals after the Clock or parameter event."""
    for schedule in schedules:
        if schedule.variability == "changing" and schedule.value_reference not in activated_clocks:
            continue
        is_aperiodic = schedule.variability in _APERIODIC_VARIABILITIES
        is_tunable_periodic = (
            refresh_tunable_clocks
            and schedule.variability == "tunable"
            and schedule.interval_from_fmu
        )
        if not is_aperiodic and not is_tunable_periodic:
            continue
        qualifier, interval = read_fmi3_clock_interval_update(
            fmu,
            schedule.value_reference,
            supports_fraction=schedule.supports_fraction,
        )
        if qualifier == FMI3_INTERVAL_NOT_YET_KNOWN:
            if schedule.variability != "countdown":
                raise RuntimeError("FMU returned NotYetKnown for a Clock that is not countdown")
            schedule.interval = None
            schedule.next_time = None
            continue
        if qualifier != FMI3_INTERVAL_CHANGED:
            if schedule.variability == "countdown" and schedule.interval is not None:
                # Countdown intervals are measured from the current Event Mode
                # instant, even when the interval value itself is unchanged.
                schedule.next_time = current_time + schedule.interval
            continue
        if interval is None:
            raise RuntimeError("FMU marked a Clock interval as changed without returning a value")
        if not math.isfinite(interval) or interval < (0 if is_aperiodic else 1e-15):
            raise RuntimeError("FMU returned an invalid Clock interval")
        schedule.interval = interval
        if is_tunable_periodic:
            if schedule.has_ticked:
                schedule.next_time = max(current_time, schedule.last_tick_time + interval)
        else:
            schedule.next_time = current_time + interval


def _read_declared_interval(variable: Any, reference: int) -> float:
    raw_decimal = _clock_attribute(variable, "intervalDecimal")
    if raw_decimal is not None:
        return _finite_number(raw_decimal, f"FMI 3 input Clock {reference} interval")

    raw_counter = _clock_attribute(variable, "intervalCounter")
    resolution = _clock_attribute(variable, "resolution")
    if raw_counter is not None and resolution:
        counter = _positive_integer(raw_counter, f"FMI 3 input Clock {reference} intervalCounter")
        divisor = _positive_integer(resolution, f"FMI 3 input Clock {reference} resolution")
        return counter / divisor

    raise ValueError(f"FMI 3 input Clock {reference} has no declared interval")


def _read_interval_from_fmu(
    fmu: Any,
    reference: int,
    *,
    supports_fraction: bool,
) -> float | None:
    if supports_fraction and hasattr(fmu, "getIntervalFraction"):
        from fmpy.fmi3 import fmi3IntervalQualifier, fmi3UInt64, fmi3ValueReference

        references = (fmi3ValueReference * 1)(reference)
        counters = (fmi3UInt64 * 1)()
        resolutions = (fmi3UInt64 * 1)()
        qualifiers = (fmi3IntervalQualifier * 1)()
        fmu.getIntervalFraction(references, counters, resolutions, qualifiers)
        qualifier = _validate_interval_qualifier(qualifiers[0], reference)
        if qualifier == FMI3_INTERVAL_NOT_YET_KNOWN:
            return None
        if qualifier == FMI3_INTERVAL_UNCHANGED:
            raise ValueError(f"FMI 3 input Clock {reference} has no initial interval value")
        if int(resolutions[0]) <= 0:
            raise ValueError(f"FMI 3 input Clock {reference} returned an invalid fractional interval")
        return int(counters[0]) / int(resolutions[0])

    if not hasattr(fmu, "getIntervalDecimal"):
        raise ValueError(f"FMI 3 input Clock {reference} has no interval getter")
    from fmpy.fmi3 import fmi3Float64, fmi3IntervalQualifier, fmi3ValueReference

    references = (fmi3ValueReference * 1)(reference)
    intervals = (fmi3Float64 * 1)()
    qualifiers = (fmi3IntervalQualifier * 1)()
    fmu.getIntervalDecimal(references, intervals, qualifiers)
    qualifier = _validate_interval_qualifier(qualifiers[0], reference)
    if qualifier == FMI3_INTERVAL_NOT_YET_KNOWN:
        return None
    if qualifier == FMI3_INTERVAL_UNCHANGED:
        raise ValueError(f"FMI 3 input Clock {reference} has no initial interval value")
    return _finite_number(intervals[0], f"FMI 3 input Clock {reference} interval")


def _read_shift(variable: Any, reference: int) -> float:
    raw_decimal = _clock_attribute(variable, "shiftDecimal")
    if raw_decimal is not None:
        return _finite_number(raw_decimal, f"FMI 3 input Clock {reference} shift")

    raw_counter = _clock_attribute(variable, "shiftCounter")
    resolution = _clock_attribute(variable, "resolution")
    if raw_counter is not None and resolution:
        counter = _positive_integer(raw_counter, f"FMI 3 input Clock {reference} shiftCounter", allow_zero=True)
        divisor = _positive_integer(resolution, f"FMI 3 input Clock {reference} resolution")
        return counter / divisor

    return 0.0


def _read_shift_from_fmu(fmu: Any, reference: int, *, supports_fraction: bool) -> float:
    if supports_fraction and hasattr(fmu, "getShiftFraction"):
        from fmpy.fmi3 import fmi3UInt64, fmi3ValueReference

        references = (fmi3ValueReference * 1)(reference)
        counters = (fmi3UInt64 * 1)()
        resolutions = (fmi3UInt64 * 1)()
        fmu.getShiftFraction(references, counters, resolutions)
        if int(resolutions[0]) <= 0:
            raise ValueError(f"FMI 3 input Clock {reference} returned an invalid fractional shift")
        return int(counters[0]) / int(resolutions[0])

    if not hasattr(fmu, "getShiftDecimal"):
        raise ValueError(f"FMI 3 input Clock {reference} has no shift getter")
    from fmpy.fmi3 import fmi3Float64, fmi3ValueReference

    references = (fmi3ValueReference * 1)(reference)
    shifts = (fmi3Float64 * 1)()
    fmu.getShiftDecimal(references, shifts)
    return _finite_number(shifts[0], f"FMI 3 input Clock {reference} shift")


def _set_interval(fmu: Any, reference: int, interval: float) -> None:
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError(f"FMI 3 input Clock {reference} interval must be positive and finite")
    setter = getattr(fmu, "setIntervalDecimal", None)
    if setter is None:
        raise ValueError(f"FMI 3 input Clock {reference} has no interval setter")
    from fmpy.fmi3 import fmi3Float64, fmi3ValueReference

    references = (fmi3ValueReference * 1)(reference)
    intervals = (fmi3Float64 * 1)(interval)
    setter(references, intervals)


def _clock_attribute(variable: Any, name: str) -> Any:
    value = getattr(variable, name, None)
    if value is None:
        value = getattr(getattr(variable, "declaredType", None), name, None)
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{label} is not finite")
    return normalized


def _positive_integer(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if normalized != value or normalized < (0 if allow_zero else 1):
        raise ValueError(f"{label} is invalid")
    return normalized


def _validate_interval_qualifier(value: Any, reference: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"FMI 3 input Clock {reference} returned an invalid interval qualifier")
    try:
        qualifier = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"FMI 3 input Clock {reference} returned an invalid interval qualifier") from exc
    if qualifier not in {
        FMI3_INTERVAL_NOT_YET_KNOWN,
        FMI3_INTERVAL_UNCHANGED,
        FMI3_INTERVAL_CHANGED,
    }:
        raise ValueError(f"FMI 3 input Clock {reference} returned an invalid interval qualifier")
    return qualifier
