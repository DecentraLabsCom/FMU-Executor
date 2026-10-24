from pathlib import Path

import pytest

from app import engine


def session_with_outputs(outputs):
    session = engine.FmuSession("batch", Path("unused.fmu"))
    session._slave = object()
    session._initialised = True
    session._state = "initialized"
    session._stop_time = 10
    session._step_size = 0.1
    session._read_outputs = lambda refs=None: outputs
    def step(_slave, size):
        return session._time + size, {"terminateSimulation": False, "eventOccurred": False}
    session._do_step = step
    return session


def test_advance_returns_every_step_and_resumes_without_duplicates():
    session = session_with_outputs({"x": 1.0})
    first = session.advance(1, max_samples=3)
    assert [round(s["time"], 2) for s in first["samples"]] == [0.1, 0.2, 0.3]
    assert first["finished"] is False
    second = session.advance(1, max_samples=3)
    assert [round(s["time"], 2) for s in second["samples"]] == [0.4, 0.5, 0.6]


def test_advance_bounds_long_output_names_by_encoded_frame_size():
    import json
    session = session_with_outputs({f"{index}:" + "a" * 250: 1.234e300 for index in range(512)})
    result = session.advance(10, max_samples=256)
    assert 0 < len(result["samples"]) < 256
    assert len(json.dumps(result).encode("utf-8")) < 1_000_000


@pytest.mark.parametrize("count", [0, -1, 257, True, 1.5])
def test_advance_rejects_invalid_batch_sizes(count):
    with pytest.raises(ValueError):
        session_with_outputs({"x": 1}).advance(1, max_samples=count)


def test_advance_stops_exactly_at_requested_time():
    session = session_with_outputs({"x": 1})
    result = session.advance(0.25, max_samples=10)
    assert result["simTime"] == pytest.approx(0.25)
    assert len(result["samples"]) == 3
