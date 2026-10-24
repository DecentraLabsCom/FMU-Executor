from __future__ import annotations

import ctypes
import os
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app import isolation


class NativeFunction:
    def __init__(self, result: Any = 0):
        self.result = result
        self.calls = []
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        self.calls.append(args)
        if callable(self.result):
            return self.result(*args)
        return self.result


class FakeKernel:
    def __init__(self, *, open_process=22, success=True):
        self.CreateJobObjectW = NativeFunction(11)
        self.SetInformationJobObject = NativeFunction(int(success))
        self.OpenProcess = NativeFunction(open_process)
        self.AssignProcessToJobObject = NativeFunction(int(success))
        self.TerminateJobObject = NativeFunction(1)
        self.CloseHandle = NativeFunction(1)


def test_capability_profiles_follow_the_host(monkeypatch):
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    assert isolation.worker_isolation_capabilities()["networkDisabled"] is True
    monkeypatch.setattr(isolation.os, "name", "nt")
    monkeypatch.setattr(isolation.sys, "platform", "win32")
    windows = isolation.worker_isolation_capabilities()
    assert windows["mechanisms"] == ["windows-app-container", "windows-job-object", "private-working-directory"]
    assert windows["networkDisabled"] and windows["readAccessRestricted"]
    assert windows["appContainerToken"] is True
    monkeypatch.setattr(isolation.os, "name", "posix")
    monkeypatch.setattr(isolation.sys, "platform", "darwin")
    profile = isolation.worker_isolation_capabilities()
    assert profile["mechanisms"] == ["rlimit"]
    assert profile["failClosed"] is False


def test_bind_worker_to_parent_handles_linux_supervisor_lifetime(monkeypatch):
    libc = SimpleNamespace(prctl=NativeFunction(0))
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    monkeypatch.setattr(isolation.os, "getppid", lambda: 123)
    isolation.bind_worker_to_parent(123)
    assert libc.prctl.calls[0][0] == 1

    libc.prctl = NativeFunction(-1)
    with pytest.raises(isolation.WorkerIsolationError):
        isolation.bind_worker_to_parent(123)


def test_bind_worker_kills_child_when_parent_changed(monkeypatch):
    libc = SimpleNamespace(prctl=NativeFunction(0))
    killed = Mock()
    exited = Mock()
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    monkeypatch.setattr(isolation.os, "getppid", lambda: 999)
    monkeypatch.setattr(isolation.os, "getpid", lambda: 456)
    monkeypatch.setattr(isolation.os, "kill", killed)
    monkeypatch.setattr(isolation.os, "_exit", exited)
    isolation.bind_worker_to_parent(123)
    killed.assert_called_once_with(456, 9)
    exited.assert_called_once_with(1)


def test_worker_process_is_alive_handles_posix_and_windows(monkeypatch):
    assert isolation.worker_process_is_alive(0) is False
    monkeypatch.setattr(isolation.os, "name", "posix")
    monkeypatch.setattr(isolation.os, "kill", Mock())
    assert isolation.worker_process_is_alive(42) is True
    monkeypatch.setattr(isolation.os, "kill", Mock(side_effect=PermissionError()))
    assert isolation.worker_process_is_alive(42) is True
    monkeypatch.setattr(isolation.os, "kill", Mock(side_effect=ProcessLookupError()))
    assert isolation.worker_process_is_alive(42) is False

    kernel = SimpleNamespace(
        OpenProcess=NativeFunction(77),
        GetExitCodeProcess=NativeFunction(lambda _handle, output: setattr(ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32)).contents, "value", 259) or 1),
        CloseHandle=NativeFunction(1),
    )
    monkeypatch.setattr(isolation.os, "name", "nt")
    monkeypatch.setattr(isolation.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    assert isolation.worker_process_is_alive(42) is True
    assert kernel.CloseHandle.calls

    kernel.OpenProcess = NativeFunction(0)
    monkeypatch.setattr(isolation.ctypes, "get_last_error", lambda: 5, raising=False)
    assert isolation.worker_process_is_alive(42) is True


def test_apply_worker_limits_sets_rlimits_and_fails_closed(monkeypatch, tmp_path):
    limits = ModuleType("resource")
    setattr(limits, "RLIMIT_CPU", 1)
    setattr(limits, "RLIMIT_AS", 2)
    setattr(limits, "RLIMIT_FSIZE", 3)
    setattr(limits, "RLIMIT_NOFILE", 4)
    setattr(limits, "RLIMIT_CORE", 5)
    setattr(limits, "setrlimit", Mock())
    monkeypatch.setitem(sys.modules, "resource", limits)
    monkeypatch.setattr(isolation.os, "name", "posix")
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    landlock = Mock()
    seccomp = Mock()
    monkeypatch.setattr(isolation, "_install_linux_landlock", landlock)
    monkeypatch.setattr(isolation, "_install_linux_worker_filter", seccomp)
    isolation.apply_worker_limits(
        {"maxCpuSeconds": 3, "maxMemoryMiB": 64},
        str(tmp_path), str(tmp_path / "model.fmu"),
    )
    assert limits.setrlimit.call_count == 5
    landlock.assert_called_once()
    seccomp.assert_called_once_with(allow_spawn=False)

    with pytest.raises(isolation.WorkerIsolationError, match="no private work directory"):
        isolation.apply_worker_limits({}, None, "model.fmu")


def test_install_linux_landlock_adds_read_and_work_directory_rules(monkeypatch, tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    fmu_path = worker_dir / "model.fmu"
    fmu_path.write_bytes(b"fmu")
    syscall_values = iter([3, 10] + [0] * 100)
    libc = SimpleNamespace(syscall=NativeFunction(lambda *_args: next(syscall_values)), prctl=NativeFunction(0))
    descriptor = iter(range(100, 1000))
    opened = Mock(side_effect=lambda *_args, **_kwargs: next(descriptor))
    closed = Mock()
    monkeypatch.setattr(isolation.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    monkeypatch.setattr(isolation.os, "open", opened)
    monkeypatch.setattr(isolation.os, "close", closed)

    isolation._install_linux_landlock(str(worker_dir), str(fmu_path))

    assert len(libc.syscall.calls) >= 8
    assert libc.prctl.calls
    assert closed.call_count == opened.call_count + 1


def test_install_linux_landlock_rejects_missing_kernel_support(monkeypatch, tmp_path):
    libc = SimpleNamespace(syscall=NativeFunction(0), prctl=NativeFunction(0))
    monkeypatch.setattr(isolation.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    with pytest.raises(isolation.WorkerIsolationError, match="unavailable"):
        isolation._install_linux_landlock(str(tmp_path), str(tmp_path / "model.fmu"))


def test_install_linux_filter_enforces_architecture_and_kernel_calls(monkeypatch):
    libc = SimpleNamespace(prctl=NativeFunction(0))
    monkeypatch.setattr(isolation.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(isolation.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    isolation._install_linux_worker_filter()
    assert len(libc.prctl.calls) == 2

    monkeypatch.setattr(isolation.platform, "machine", lambda: "mips")
    with pytest.raises(isolation.WorkerIsolationError, match="unsupported"):
        isolation._install_linux_worker_filter()


def test_windows_job_object_sets_limits_and_closes_handles(monkeypatch):
    kernel = FakeKernel()
    monkeypatch.setattr(isolation.os, "name", "nt")
    monkeypatch.setattr(isolation.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    job = isolation.WindowsWorkerJob(99, {"maxCpuSeconds": 5, "maxMemoryMiB": 128})
    job.terminate(7)
    job.close()
    assert kernel.SetInformationJobObject.calls
    assert kernel.AssignProcessToJobObject.calls
    assert kernel.TerminateJobObject.calls[0][1] == 7
    assert len(kernel.CloseHandle.calls) == 2


def test_windows_job_object_fails_closed_when_configuration_fails(monkeypatch):
    kernel = FakeKernel(success=False)
    monkeypatch.setattr(isolation.os, "name", "nt")
    monkeypatch.setattr(isolation.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    with pytest.raises(isolation.WorkerIsolationError, match="configure"):
        isolation.WindowsWorkerJob(99)
    assert kernel.CloseHandle.calls
