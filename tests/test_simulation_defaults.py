from app.simulation_defaults import resolve_simulation_time_options


def test_resolves_fmu_defaults_before_executor_fallbacks():
    assert resolve_simulation_time_options({
        "defaultStartTime": 2.0,
        "defaultStopTime": 12.0,
        "defaultStepSize": 0.5,
    }) == {
        "startTime": 2.0,
        "stopTime": 12.0,
        "stepSize": 0.5,
    }


def test_fills_missing_or_invalid_defaults_from_executor_contract():
    assert resolve_simulation_time_options({
        "defaultStartTime": 3.0,
        "defaultStopTime": 2.0,
        "defaultStepSize": 0.0,
    }) == {
        "startTime": 3.0,
        "stopTime": 4.0,
        "stepSize": 0.001,
    }
    assert resolve_simulation_time_options({}) == {
        "startTime": 0.0,
        "stopTime": 1.0,
        "stepSize": 0.001,
    }


def test_explicit_request_values_override_fmu_defaults():
    assert resolve_simulation_time_options(
        {"defaultStartTime": 2.0, "defaultStopTime": 12.0, "defaultStepSize": 0.5},
        {"startTime": 0.0, "stopTime": 5.0, "stepSize": 0.25},
    ) == {"startTime": 0.0, "stopTime": 5.0, "stepSize": 0.25}
