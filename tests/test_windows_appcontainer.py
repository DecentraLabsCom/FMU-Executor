from __future__ import annotations

import ctypes
import multiprocessing
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from app import windows_appcontainer as appcontainer
from app.isolation import WorkerIsolationError


class NativeFunction:
    def __init__(self, result: Any = 1):
        self.result = result
        self.calls = []
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        self.calls.append(args)
        if callable(self.result):
            return self.result(*args)
        return self.result


def set_appcontainer_platform(monkeypatch, name: str) -> None:
    """Select the platform for this module without changing the host process."""
    monkeypatch.setattr(appcontainer, "os", SimpleNamespace(name=name, environ=os.environ))


def profile_apis(*, profile_result=0, convert_result=1, delete_result=0):
    sid_buffer = ctypes.create_unicode_buffer("S-1-15-2-987")

    def create_profile(_name, _display, _description, _capabilities, _count, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 1234
        return profile_result

    def convert_sid(_sid, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(sid_buffer)
        return convert_result

    userenv = SimpleNamespace(
        CreateAppContainerProfile=NativeFunction(create_profile),
        DeleteAppContainerProfile=NativeFunction(delete_result),
    )
    kernel = SimpleNamespace(LocalFree=NativeFunction(1))
    advapi = SimpleNamespace(
        FreeSid=NativeFunction(1),
        ConvertSidToStringSidW=NativeFunction(convert_sid),
    )
    return {"userenv": userenv, "kernel32": kernel, "advapi32": advapi}


def test_runtime_directories_cover_frozen_and_python_installs(monkeypatch, tmp_path):
    frozen_root = tmp_path / "bundle"
    frozen_root.mkdir()
    extraction = tmp_path / "extract"
    extraction.mkdir()
    monkeypatch.setattr(appcontainer.sys, "frozen", True, raising=False)
    monkeypatch.setattr(appcontainer.sys, "_MEIPASS", str(extraction), raising=False)
    monkeypatch.setattr(appcontainer.sys, "executable", str(frozen_root / "executor.exe"))
    assert appcontainer._runtime_directories() == {frozen_root, extraction}

    monkeypatch.setattr(appcontainer.sys, "frozen", False, raising=False)
    directories = appcontainer._runtime_directories()
    assert Path(appcontainer.__file__).resolve().parent in directories
    assert Path(sys.executable).resolve().parent in directories


def test_icacls_reports_process_and_command_failures(monkeypatch, tmp_path):
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(appcontainer.subprocess, "run", run)
    appcontainer._icacls(tmp_path, "/grant", "SID:(RX)")
    assert run.call_args.args[0][-2:] == ["/grant", "SID:(RX)"]
    run.return_value.returncode = 1
    with pytest.raises(WorkerIsolationError, match="access rules"):
        appcontainer._icacls(tmp_path, "/grant", "SID:(RX)")
    run.side_effect = OSError("missing icacls")
    with pytest.raises(WorkerIsolationError, match="access rules"):
        appcontainer._icacls(tmp_path, "/grant", "SID:(RX)")


def test_grant_runtime_read_access_applies_package_ace(monkeypatch, tmp_path):
    directory = tmp_path / "runtime"
    directory.mkdir()
    grants = Mock()
    monkeypatch.setattr(appcontainer, "_runtime_directories", lambda: {directory})
    monkeypatch.setattr(appcontainer, "_icacls", grants)
    appcontainer._grant_runtime_read_access()
    grants.assert_called_once_with(directory, "/grant", "*S-1-15-2-1:(OI)(CI)(RX)", "/T", "/Q")


def test_appcontainer_creates_profile_grants_access_and_closes(monkeypatch, tmp_path):
    worker_directory = tmp_path / "worker_test-1"
    worker_directory.mkdir()
    apis = profile_apis()
    set_appcontainer_platform(monkeypatch, "nt")
    monkeypatch.setattr(appcontainer.ctypes, "WinDLL", lambda name, **_kwargs: apis[name], raising=False)
    runtime_grant = Mock()
    icacls = Mock()
    monkeypatch.setattr(appcontainer, "_grant_runtime_read_access", runtime_grant)
    monkeypatch.setattr(appcontainer, "_icacls", icacls)

    container = appcontainer.WindowsAppContainer(worker_directory)

    assert container.name == "DecentraLabs.FMU.worker_test-1"
    runtime_grant.assert_called_once_with()
    assert icacls.call_count == 2
    assert "S-1-15-2-987" in icacls.call_args_list[0].args[2]
    container.close()
    container.close()
    assert len(apis["advapi32"].FreeSid.calls) == 1
    assert len(apis["userenv"].DeleteAppContainerProfile.calls) == 1


def test_appcontainer_rejects_unsupported_host_names_and_profile_errors(monkeypatch, tmp_path):
    set_appcontainer_platform(monkeypatch, "posix")
    with pytest.raises(WorkerIsolationError, match="only available"):
        appcontainer.WindowsAppContainer(tmp_path)

    set_appcontainer_platform(monkeypatch, "nt")
    invalid_name = tmp_path / ("x" * 60)
    invalid_name.mkdir()
    with pytest.raises(WorkerIsolationError, match="cannot identify"):
        appcontainer.WindowsAppContainer(invalid_name)

    worker_directory = tmp_path / "worker_error"
    worker_directory.mkdir()
    apis = profile_apis(profile_result=0x80070005)
    monkeypatch.setattr(appcontainer.ctypes, "WinDLL", lambda name, **_kwargs: apis[name], raising=False)
    with pytest.raises(WorkerIsolationError, match="create Windows FMU AppContainer"):
        appcontainer.WindowsAppContainer(worker_directory)


def test_appcontainer_cleans_up_if_sid_conversion_or_acl_setup_fails(monkeypatch, tmp_path):
    worker_directory = tmp_path / "worker_failure"
    worker_directory.mkdir()
    set_appcontainer_platform(monkeypatch, "nt")
    apis = profile_apis(convert_result=0)
    monkeypatch.setattr(appcontainer.ctypes, "WinDLL", lambda name, **_kwargs: apis[name], raising=False)
    with pytest.raises(WorkerIsolationError, match="read the Windows FMU"):
        appcontainer.WindowsAppContainer(worker_directory)
    assert apis["advapi32"].FreeSid.calls
    assert apis["userenv"].DeleteAppContainerProfile.calls

    apis = profile_apis()
    monkeypatch.setattr(appcontainer.ctypes, "WinDLL", lambda name, **_kwargs: apis[name], raising=False)
    monkeypatch.setattr(appcontainer, "_grant_runtime_read_access", Mock(side_effect=WorkerIsolationError("acl failed")))
    with pytest.raises(WorkerIsolationError, match="acl failed"):
        appcontainer.WindowsAppContainer(worker_directory)
    assert apis["userenv"].DeleteAppContainerProfile.calls


@pytest.mark.parametrize(
    "environment",
    [None, {"B": "2", "A": "1", "TEMP": "C:\\old-temp"}],
)
def test_create_process_uses_private_environment_and_explicit_appcontainer(monkeypatch, tmp_path, environment):
    observed_environment = {}

    def initialize_attribute_list(attribute_list, _count, _flags, size_pointer):
        if attribute_list is None:
            ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = 128
            return 0
        return 1

    def create_process(*args):
        environment_buffer = ctypes.cast(args[6], ctypes.POINTER(ctypes.c_wchar))
        entries = []
        entry = []
        for index in range(65536):
            character = environment_buffer[index]
            if character == "\0":
                if not entry:
                    break
                entries.append("".join(entry))
                entry = []
            else:
                entry.append(character)
        observed_environment.update(dict(item.split("=", 1) for item in entries))
        info = ctypes.cast(args[9], ctypes.POINTER(appcontainer._ProcessInformation)).contents
        info.hProcess, info.hThread = 11, 12
        info.dwProcessId, info.dwThreadId = 13, 14
        return 1

    kernel = SimpleNamespace(
        InitializeProcThreadAttributeList=NativeFunction(initialize_attribute_list),
        UpdateProcThreadAttribute=NativeFunction(1),
        DeleteProcThreadAttributeList=NativeFunction(None),
        SetHandleInformation=NativeFunction(1),
        CreateProcessW=NativeFunction(create_process),
    )
    monkeypatch.setattr(appcontainer.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    container = appcontainer.WindowsAppContainer.__new__(appcontainer.WindowsAppContainer)
    container.worker_directory = tmp_path
    container._sid = ctypes.c_void_p(1234)
    result = container.create_process(
        object(), "python.exe", "python.exe -c pipe_handle=42", None, None,
        True, 0, environment, None, SimpleNamespace(dwFlags=7),
    )
    assert result == (11, 12, 13, 14)
    assert kernel.SetHandleInformation.calls[0][0] == 42
    assert len(kernel.UpdateProcThreadAttribute.calls) == 2
    assert kernel.DeleteProcThreadAttributeList.calls
    assert kernel.CreateProcessW.calls[0][5] & 0x00000400  # CREATE_UNICODE_ENVIRONMENT
    private_path = str(tmp_path)
    for variable in ("TMP", "TEMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME"):
        assert observed_environment[variable] == private_path
    if environment is None:
        assert "PATH" in {key.upper() for key in observed_environment}
    else:
        assert observed_environment["A"] == "1"
        assert observed_environment["B"] == "2"


def test_appcontainer_pipe_reducer_duplicates_worker_handles_and_restores(monkeypatch, tmp_path):
    import multiprocessing.connection as connection_module
    import multiprocessing.context as context_module
    import multiprocessing.reduction as reduction_module

    class FakePipe:
        readable = True
        writable = False

        @staticmethod
        def fileno():
            return 55

    original_reducer = Mock(return_value=("original", ()))
    registrations = []
    fake_reduction = SimpleNamespace(register=lambda pipe_type, reducer: registrations.append((pipe_type, reducer)))
    monkeypatch.setattr(connection_module, "PipeConnection", FakePipe, raising=False)
    monkeypatch.setattr(connection_module, "rebuild_pipe_connection", Mock(), raising=False)
    monkeypatch.setattr(
        connection_module,
        "_winapi",
        SimpleNamespace(FILE_GENERIC_READ=1, FILE_GENERIC_WRITE=2),
        raising=False,
    )
    monkeypatch.setattr(connection_module, "reduce_pipe_connection", original_reducer, raising=False)
    monkeypatch.setattr(connection_module, "reduction", fake_reduction, raising=False)
    duplicate_handle = Mock(return_value="duplicated-handle")
    monkeypatch.setattr(reduction_module, "DupHandle", duplicate_handle, raising=False)
    monkeypatch.setattr(context_module, "get_spawning_popen", lambda: SimpleNamespace(pid=987))

    container = SimpleNamespace(close=Mock(), create_process=Mock(return_value="isolated"))
    monkeypatch.setattr(appcontainer, "WindowsAppContainer", lambda _directory: container)
    original_winapi = SimpleNamespace(CreateProcess=Mock(return_value="ordinary"))
    spawn_module = ModuleType("multiprocessing.popen_spawn_win32")
    spawn_module._winapi = original_winapi
    spawn_module.spawn = SimpleNamespace(get_command_line=lambda **_kwargs: [])
    captured = []

    def popen(_process):
        captured.append(connection_module.reduce_pipe_connection(FakePipe()))
        return SimpleNamespace(pid=987)

    spawn_module.Popen = popen
    monkeypatch.setitem(sys.modules, "multiprocessing.popen_spawn_win32", spawn_module)
    monkeypatch.setattr(multiprocessing, "popen_spawn_win32", spawn_module, raising=False)
    process = SimpleNamespace(_fmu_worker_directory=str(tmp_path / "worker_test"))

    assert appcontainer._appcontainer_popen(process).pid == 987
    assert duplicate_handle.call_args.args == (55, 1)
    assert duplicate_handle.call_args.kwargs == {"pid": 987}
    assert captured == [(connection_module.rebuild_pipe_connection, ("duplicated-handle", True, False))]
    assert connection_module.reduce_pipe_connection is original_reducer
    assert registrations[0][0] is FakePipe
    assert registrations[0][1] is not original_reducer
    assert registrations[-1] == (FakePipe, original_reducer)


def test_create_process_rejects_missing_security_and_bootstrap_state(monkeypatch, tmp_path):
    def initialize_attribute_list(attribute_list, _count, _flags, size_pointer):
        if attribute_list is None:
            ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = 128
            return 0
        return 1

    kernel = SimpleNamespace(
        InitializeProcThreadAttributeList=NativeFunction(initialize_attribute_list),
        UpdateProcThreadAttribute=NativeFunction(1),
        DeleteProcThreadAttributeList=NativeFunction(None),
        SetHandleInformation=NativeFunction(1),
        CreateProcessW=NativeFunction(1),
    )
    monkeypatch.setattr(appcontainer.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    container = appcontainer.WindowsAppContainer.__new__(appcontainer.WindowsAppContainer)
    container.worker_directory = tmp_path
    container._sid = ctypes.c_void_p(1234)
    with pytest.raises(WorkerIsolationError, match="security attributes"):
        container.create_process(None, "python", "python", object(), None, True, 0, None, None, None)
    with pytest.raises(WorkerIsolationError, match="bootstrap pipe"):
        container.create_process(None, "python", "python", None, None, False, 0, None, None, None)
    with pytest.raises(WorkerIsolationError, match="required isolation"):
        container._sid = None
        container.create_process(None, "python", "python", None, None, True, 0, None, None, None)


def test_create_process_fails_closed_for_missing_pipe_and_winapi_errors(monkeypatch, tmp_path):
    def initialize_attribute_list(attribute_list, _count, _flags, size_pointer):
        ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = 128
        return 0 if attribute_list is None else 1

    kernel = SimpleNamespace(
        InitializeProcThreadAttributeList=NativeFunction(initialize_attribute_list),
        UpdateProcThreadAttribute=NativeFunction(1),
        DeleteProcThreadAttributeList=NativeFunction(None),
        SetHandleInformation=NativeFunction(1),
        CreateProcessW=NativeFunction(1),
    )
    monkeypatch.setattr(appcontainer.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(appcontainer.ctypes, "WinError", lambda *_args: OSError("winapi error"), raising=False)
    monkeypatch.setattr(appcontainer.ctypes, "get_last_error", lambda: 5, raising=False)
    container = appcontainer.WindowsAppContainer.__new__(appcontainer.WindowsAppContainer)
    container.worker_directory = tmp_path
    container._sid = ctypes.c_void_p(1234)
    with pytest.raises(WorkerIsolationError, match="bootstrap pipe"):
        container.create_process(None, "python", "python.exe -c no_pipe", None, None, True, 0, None, None, None)
    assert kernel.DeleteProcThreadAttributeList.calls

    kernel.InitializeProcThreadAttributeList = NativeFunction(lambda *_args: 0)
    with pytest.raises(OSError, match="winapi error"):
        container.create_process(None, "python", "python.exe", None, None, True, 0, None, None, None)


def test_delete_worker_profile_validates_platform_name_and_logs_failure(monkeypatch):
    set_appcontainer_platform(monkeypatch, "posix")
    appcontainer.delete_worker_appcontainer_profile("worker_123")
    set_appcontainer_platform(monkeypatch, "nt")
    appcontainer.delete_worker_appcontainer_profile("unrelated")
    appcontainer.delete_worker_appcontainer_profile("worker_" + "x" * 70)

    userenv = SimpleNamespace(DeleteAppContainerProfile=NativeFunction(0x80070005))
    monkeypatch.setattr(appcontainer.ctypes, "WinDLL", lambda *_args, **_kwargs: userenv, raising=False)
    appcontainer.delete_worker_appcontainer_profile("realtime_123")
    assert userenv.DeleteAppContainerProfile.calls[0][0] == "DecentraLabs.FMU.realtime_123"


def test_winapi_proxy_scopes_process_creation_to_current_worker():
    original = SimpleNamespace(CreateProcess=Mock(return_value="ordinary"), marker="forwarded")
    container = SimpleNamespace(create_process=Mock(return_value="isolated"))
    proxy = appcontainer._WinApiProxy(original, cast(Any, container))
    assert proxy.marker == "forwarded"
    assert proxy.CreateProcess("args") == "ordinary"
    appcontainer._SPAWN_LOCAL.appcontainer = container
    try:
        assert proxy.CreateProcess("args") == "isolated"
        container.create_process.assert_called_once_with(original, "args")
    finally:
        appcontainer._SPAWN_LOCAL.appcontainer = None


def test_appcontainer_popen_restores_global_spawn_state_on_success_and_failure(monkeypatch, tmp_path):
    container = SimpleNamespace(close=Mock(), create_process=Mock(return_value="isolated"))
    monkeypatch.setattr(appcontainer, "WindowsAppContainer", lambda _directory: container)
    original = SimpleNamespace(CreateProcess=Mock(return_value="ordinary"))
    calls = []

    def popen(process):
        module = sys.modules["multiprocessing.popen_spawn_win32"]
        calls.append(module._winapi.CreateProcess("worker args"))
        calls.append(module.spawn.get_command_line(parent_pid=123))
        return "popen-result"

    spawn_module = ModuleType("multiprocessing.popen_spawn_win32")
    setattr(spawn_module, "_winapi", original)
    get_command_line = Mock(return_value=["spawn-command"])
    setattr(spawn_module, "spawn", SimpleNamespace(get_command_line=get_command_line))
    setattr(spawn_module, "Popen", popen)
    monkeypatch.setitem(sys.modules, "multiprocessing.popen_spawn_win32", spawn_module)
    monkeypatch.setattr(multiprocessing, "popen_spawn_win32", spawn_module, raising=False)
    process = SimpleNamespace(_fmu_worker_directory=str(tmp_path / "worker"))
    assert appcontainer._appcontainer_popen(process) == "popen-result"
    assert calls == ["isolated", ["spawn-command"]]
    get_command_line.assert_called_once_with()
    assert getattr(spawn_module, "_winapi") is original
    assert spawn_module.spawn.get_command_line is get_command_line
    assert process._fmu_appcontainer is container
    assert appcontainer._SPAWN_LOCAL.appcontainer is None

    container = SimpleNamespace(close=Mock(), create_process=Mock())
    monkeypatch.setattr(appcontainer, "WindowsAppContainer", lambda _directory: container)
    setattr(spawn_module, "Popen", Mock(side_effect=RuntimeError("spawn failed")))
    with pytest.raises(RuntimeError, match="spawn failed"):
        appcontainer._appcontainer_popen(process)
    container.close.assert_called_once_with()
    assert getattr(spawn_module, "_winapi") is original
    assert spawn_module.spawn.get_command_line is get_command_line
    assert appcontainer._SPAWN_LOCAL.appcontainer is None


def test_appcontainer_process_requires_private_worker_directory():
    process = object.__new__(appcontainer.WindowsAppContainerProcess)
    with pytest.raises(WorkerIsolationError, match="private working directory"):
        appcontainer._appcontainer_popen(process)

    process = appcontainer.WindowsAppContainerProcess(target=lambda: None, worker_directory="worker_test")
    assert process._fmu_worker_directory == "worker_test"
    assert process._fmu_appcontainer is None
