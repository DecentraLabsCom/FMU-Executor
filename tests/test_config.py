from __future__ import annotations

import base64

import pytest

from app import config


def test_required_environment_variable_must_be_nonempty(monkeypatch):
    monkeypatch.delenv("FMU_TEST_REQUIRED", raising=False)
    with pytest.raises(RuntimeError, match="FMU_TEST_REQUIRED"):
        config._env("FMU_TEST_REQUIRED", required=True)

    monkeypatch.setenv("FMU_TEST_REQUIRED", "configured")
    assert config._env("FMU_TEST_REQUIRED", required=True) == "configured"


def test_internal_token_supports_raw_and_unpadded_urlsafe_base64(monkeypatch):
    monkeypatch.delenv("FMU_INTERNAL_TOKEN_B64", raising=False)
    monkeypatch.setenv("FMU_INTERNAL_TOKEN", "raw-secret")
    assert config.internal_token() == "raw-secret"

    encoded = base64.urlsafe_b64encode(b"token-safe").decode().rstrip("=")
    monkeypatch.setenv("FMU_INTERNAL_TOKEN_B64", encoded)
    assert config.internal_token() == "token-safe"


@pytest.mark.parametrize("encoded", ["%%%", "/w"])
def test_internal_token_rejects_invalid_base64_and_invalid_utf8(monkeypatch, encoded):
    monkeypatch.setenv("FMU_INTERNAL_TOKEN_B64", encoded)
    monkeypatch.setenv("FMU_INTERNAL_TOKEN", "must-not-fallback")
    assert config.internal_token() is None


def test_network_and_logging_environment_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("FMU_EXECUTOR_HOST", raising=False)
    monkeypatch.delenv("FMU_EXECUTOR_PORT", raising=False)
    monkeypatch.delenv("FMU_LOG_LEVEL", raising=False)
    assert config.bind_host() == "0.0.0.0"
    assert config.bind_port() == 8091
    assert config.log_level() == "INFO"

    monkeypatch.setenv("FMU_EXECUTOR_HOST", "127.0.0.1")
    monkeypatch.setenv("FMU_EXECUTOR_PORT", "9001")
    monkeypatch.setenv("FMU_LOG_LEVEL", "DEBUG")
    assert config.bind_host() == "127.0.0.1"
    assert config.bind_port() == 9001
    assert config.log_level() == "DEBUG"


def test_execution_configuration_normalizes_values_and_clamps_timeouts(monkeypatch):
    monkeypatch.delenv("FMU_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("FMU_EXECUTION_TIMEOUT_SECONDS", raising=False)
    assert config.execution_mode() == "process"
    assert config.execution_timeout_seconds() == 3600

    monkeypatch.setenv("FMU_EXECUTION_MODE", " IN-PROCESS ")
    monkeypatch.setenv("FMU_EXECUTION_TIMEOUT_SECONDS", "0.25")
    assert config.execution_mode() == "in-process"
    assert config.execution_timeout_seconds() == 1

    monkeypatch.setenv("FMU_EXECUTION_MODE", "unsupported")
    monkeypatch.setenv("FMU_EXECUTION_TIMEOUT_SECONDS", "12.5")
    assert config.execution_mode() == "process"
    assert config.execution_timeout_seconds() == 12.5


def test_omsimulator_configuration_parses_boolean_and_command(monkeypatch):
    monkeypatch.delenv("FMU_OMSIMULATOR_ENABLED", raising=False)
    monkeypatch.delenv("FMU_OMSIMULATOR_COMMAND", raising=False)
    monkeypatch.delenv("FMU_OMSIMULATOR_TIMEOUT_SECONDS", raising=False)
    assert config.omsimulator_enabled() is False
    assert config.omsimulator_command() == "OMSimulator"
    assert config.omsimulator_timeout_seconds() == 3600

    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("FMU_OMSIMULATOR_ENABLED", value)
        assert config.omsimulator_enabled() is True

    monkeypatch.setenv("FMU_OMSIMULATOR_ENABLED", "no")
    monkeypatch.setenv("FMU_OMSIMULATOR_COMMAND", "omsim --worker")
    monkeypatch.setenv("FMU_OMSIMULATOR_TIMEOUT_SECONDS", "0")
    assert config.omsimulator_enabled() is False
    assert config.omsimulator_command() == "omsim --worker"
    assert config.omsimulator_timeout_seconds() == 1
