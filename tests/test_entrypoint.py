from __future__ import annotations

from app import config
from app.__main__ import main


def test_main_starts_uvicorn_with_configured_network_and_log_settings(monkeypatch):
    captured = {}

    def record_run(target, **kwargs):
        captured["target"] = target
        captured.update(kwargs)

    monkeypatch.setattr(config, "bind_host", lambda: "127.0.0.1")
    monkeypatch.setattr(config, "bind_port", lambda: 9100)
    monkeypatch.setattr(config, "log_level", lambda: "DEBUG")
    monkeypatch.setattr("uvicorn.run", record_run)

    main()

    assert captured == {
        "target": "app.main:app",
        "host": "127.0.0.1",
        "port": 9100,
        "log_level": "debug",
    }
