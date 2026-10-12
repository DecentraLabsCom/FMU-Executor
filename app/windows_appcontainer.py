"""Launch FMU worker processes in per-worker Windows AppContainers.

The sidecar may run as SYSTEM, so lowering privileges after a normal spawn is
not a security boundary. This module adds the AppContainer token at process
creation time and grants that package access only to the runtime and its
private worker directory.
"""

from __future__ import annotations

import ctypes
import logging
import multiprocessing as mp
import os
import re
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .isolation import WorkerIsolationError

logger = logging.getLogger(__name__)

_APP_CONTAINER_PREFIX = "DecentraLabs.FMU."
_APP_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SPAWN_LOCK = threading.RLock()
_SPAWN_LOCAL = threading.local()
_SPAWN_CONTEXT = mp.get_context("spawn")
_RUNTIME_ACL_LOCK = threading.Lock()
_RUNTIME_ACL_PATHS: set[str] = set()


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _icacls(path: Path, *arguments: str) -> None:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = system_root / "System32" / "icacls.exe"
    try:
        result = subprocess.run(
            [str(executable), str(path), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise WorkerIsolationError("Unable to configure Windows FMU worker access rules") from exc
    if result.returncode != 0:
        raise WorkerIsolationError("Unable to configure Windows FMU worker access rules")


def _runtime_directories() -> set[Path]:
    """Return code/runtime directories that an AppContainer worker must read."""
    if getattr(sys, "frozen", False):
        roots = {Path(sys.executable).resolve().parent}
        extraction_root = getattr(sys, "_MEIPASS", None)
        if extraction_root:
            roots.add(Path(extraction_root).resolve())
        candidates = {path for path in roots if path.is_dir()}
    else:
        candidates = {
            Path(__file__).resolve().parent,
            Path(sys.executable).resolve().parent,
            Path(sys.prefix).resolve() / "Lib",
            Path(sys.prefix).resolve() / "DLLs",
            Path(sys.base_prefix).resolve() / "Lib",
            Path(sys.base_prefix).resolve() / "DLLs",
        }
        base_executable = getattr(sys, "_base_executable", None)
        if base_executable:
            candidates.add(Path(base_executable).resolve().parent)
        candidates = {path for path in candidates if path.is_dir()}

    selected: list[Path] = []
    for candidate in sorted(candidates, key=lambda path: len(path.parts)):
        if any(candidate == parent or parent in candidate.parents for parent in selected):
            continue
        selected.append(candidate)
    return set(selected)


def _grant_runtime_read_access() -> None:
    # AppContainer file access requires an explicit package/capability ACE.
    # Runtime files contain executable code, not provisioned FMUs or secrets.
    directories = _runtime_directories()
    if not directories:
        raise WorkerIsolationError("No Windows FMU worker runtime paths are available")
    with _RUNTIME_ACL_LOCK:
        for directory in directories:
            key = os.path.normcase(str(directory))
            if key in _RUNTIME_ACL_PATHS:
                continue
            _icacls(directory, "/grant", "*S-1-15-2-1:(OI)(CI)(RX)", "/T", "/Q")
            _RUNTIME_ACL_PATHS.add(key)


class WindowsAppContainer:
    """Per-worker AppContainer package identity and its filesystem grants."""

    def __init__(self, worker_directory: str | Path):
        if os.name != "nt":
            raise WorkerIsolationError("Windows AppContainers are only available on Windows")
        self.worker_directory = Path(worker_directory).resolve()
        self.name = _APP_CONTAINER_PREFIX + self.worker_directory.name
        if not _APP_CONTAINER_NAME_RE.fullmatch(self.name):
            raise WorkerIsolationError("FMU worker directory cannot identify a Windows AppContainer")
        self._sid: ctypes.c_void_p | None = None
        self._closed = False
        self._userenv = ctypes.WinDLL("userenv", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self._userenv.CreateAppContainerProfile.restype = ctypes.c_long
        self._userenv.CreateAppContainerProfile.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ]
        self._userenv.DeleteAppContainerProfile.restype = ctypes.c_long
        self._userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
        self._advapi.FreeSid.restype = ctypes.c_void_p
        self._advapi.FreeSid.argtypes = [ctypes.c_void_p]
        self._kernel.LocalFree.restype = ctypes.c_void_p
        self._kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self._advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
        self._advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]

        sid = ctypes.c_void_p()
        result = self._userenv.CreateAppContainerProfile(
            self.name,
            "DecentraLabs FMU worker",
            "Isolated worker for an FMI model",
            None,
            0,
            ctypes.byref(sid),
        )
        if result != 0:
            raise WorkerIsolationError(f"Unable to create Windows FMU AppContainer (HRESULT 0x{result & 0xFFFFFFFF:08x})")
        self._sid = sid
        try:
            sid_text = self._sid_to_string(sid)
            _grant_runtime_read_access()
            _icacls(
                self.worker_directory,
                "/grant",
                f"*{sid_text}:(OI)(CI)(M)",
                "/T",
                "/Q",
            )
            _icacls(self.worker_directory, "/setintegritylevel", "(OI)(CI)L", "/T", "/Q")
        except Exception:
            self.close()
            raise

    def _sid_to_string(self, sid: ctypes.c_void_p) -> str:
        sid_string = wintypes.LPWSTR()
        if not self._advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise WorkerIsolationError("Unable to read the Windows FMU AppContainer identity")
        try:
            if not sid_string.value:
                raise WorkerIsolationError("Windows FMU AppContainer identity is empty")
            return sid_string.value
        finally:
            self._kernel.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))

    def create_process(
        self,
        original_winapi: Any,
        application_name: str,
        command_line: str,
        process_attributes: Any,
        thread_attributes: Any,
        inherit_handles: bool,
        creation_flags: int,
        environment: dict[str, str] | None,
        current_directory: str | None,
        startup_info: Any,
    ) -> tuple[int, int, int, int]:
        """Create a spawn child with an AppContainer token and explicit handles."""
        del original_winapi  # Kept in the signature to mirror _winapi.CreateProcess.
        if process_attributes is not None or thread_attributes is not None:
            raise WorkerIsolationError("Unexpected Windows process security attributes")
        if self._sid is None:
            raise WorkerIsolationError("Windows FMU worker launch is missing required isolation state")

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel.DeleteProcThreadAttributeList.restype = None
        kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel.SetHandleInformation.restype = wintypes.BOOL
        kernel.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        kernel.CreateProcessW.restype = wintypes.BOOL
        kernel.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(_StartupInfo), ctypes.POINTER(_ProcessInformation),
        ]

        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        attribute_buffer = ctypes.create_string_buffer(size.value)
        attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        if not kernel.InitializeProcThreadAttributeList(attribute_list, 2, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())

        initialized = True
        try:
            security = _SecurityCapabilities(self._sid, None, 0, 0)
            security_attribute = 0x00020009  # PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES
            if not kernel.UpdateProcThreadAttribute(
                attribute_list, 0, security_attribute, ctypes.byref(security),
                ctypes.sizeof(security), None, None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            # multiprocessing.spawn passes its one-way bootstrap pipe through
            # this handle list; all other parent handles remain non-inheritable.
            pipe_handle_match = re.search(r"pipe_handle=(\d+)", command_line)
            if pipe_handle_match is None:
                raise WorkerIsolationError("Windows FMU worker bootstrap pipe could not be isolated")
            child_pipe_handle = int(pipe_handle_match.group(1))
            handle_list = (wintypes.HANDLE * 1)(child_pipe_handle)
            if not kernel.SetHandleInformation(child_pipe_handle, 0x00000001, 0x00000001):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.UpdateProcThreadAttribute(
                attribute_list, 0, 0x00020002, ctypes.byref(handle_list),
                ctypes.sizeof(handle_list), None, None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
            startup.StartupInfo.dwFlags = int(getattr(startup_info, "dwFlags", 0))
            startup.lpAttributeList = attribute_list
            process_info = _ProcessInformation()
            command_buffer = ctypes.create_unicode_buffer(command_line)
            child_environment = None
            flags = int(creation_flags) | 0x00080000  # EXTENDED_STARTUPINFO_PRESENT
            if environment is not None:
                environment_values = dict(environment)
                private_path = str(self.worker_directory)
                for variable in ("TMP", "TEMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME"):
                    for key in tuple(environment_values):
                        if key.upper() == variable:
                            environment_values.pop(key)
                    environment_values[variable] = private_path
                entries = sorted(environment_values.items(), key=lambda item: item[0].upper())
                environment_text = "\0".join(f"{key}={value}" for key, value in entries) + "\0\0"
                child_environment = ctypes.create_unicode_buffer(environment_text)
                flags |= 0x00000400  # CREATE_UNICODE_ENVIRONMENT
            working_directory = str(self.worker_directory)
            if not kernel.CreateProcessW(
                application_name,
                command_buffer,
                None,
                None,
                True,
                flags,
                ctypes.cast(child_environment, ctypes.c_void_p) if child_environment is not None else None,
                working_directory,
                ctypes.byref(startup.StartupInfo),
                ctypes.byref(process_info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if process_info.hProcess is None or process_info.hThread is None:
                raise WorkerIsolationError("Windows FMU worker process creation returned invalid handles")
            return (
                int(process_info.hProcess),
                int(process_info.hThread),
                int(process_info.dwProcessId),
                int(process_info.dwThreadId),
            )
        finally:
            if initialized:
                kernel.DeleteProcThreadAttributeList(attribute_list)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        sid = self._sid
        self._sid = None
        if sid:
            self._advapi.FreeSid(sid)
        result = self._userenv.DeleteAppContainerProfile(self.name)
        if result != 0:
            logger.warning(
                "Unable to delete FMU worker AppContainer profile %s (HRESULT 0x%08x)",
                self.name,
                result & 0xFFFFFFFF,
            )


def delete_worker_appcontainer_profile(worker_directory_name: str) -> bool:
    """Remove profile state left by a worker after an abrupt sidecar exit."""
    if os.name != "nt":
        return True
    if not worker_directory_name.startswith(("worker_", "realtime_")):
        return True
    profile_name = _APP_CONTAINER_PREFIX + worker_directory_name
    if not _APP_CONTAINER_NAME_RE.fullmatch(profile_name):
        return False
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    result = userenv.DeleteAppContainerProfile(profile_name)
    if result != 0:
        logger.warning(
            "Unable to remove abandoned FMU worker AppContainer profile %s (HRESULT 0x%08x)",
            profile_name,
            result & 0xFFFFFFFF,
        )
        return False
    return True


class _WinApiProxy:
    """Route only the current FMU spawn through CreateProcessW AppContainer."""

    def __init__(self, original: Any, appcontainer: WindowsAppContainer):
        self._original = original
        self._appcontainer = appcontainer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)

    def CreateProcess(self, *args: Any) -> Any:
        if getattr(_SPAWN_LOCAL, "appcontainer", None) is not self._appcontainer:
            return self._original.CreateProcess(*args)
        return self._appcontainer.create_process(self._original, *args)


def _appcontainer_popen(process_obj: Any) -> Any:
    worker_directory = getattr(process_obj, "_fmu_worker_directory", None)
    if not worker_directory:
        raise WorkerIsolationError("Windows FMU worker has no private working directory")
    appcontainer = WindowsAppContainer(worker_directory)
    from multiprocessing import popen_spawn_win32
    spawn_module: Any = popen_spawn_win32

    with _SPAWN_LOCK:
        original_winapi = spawn_module._winapi
        original_get_command_line = spawn_module.spawn.get_command_line

        def appcontainer_command_line(**kwargs: Any) -> list[str]:
            if getattr(_SPAWN_LOCAL, "appcontainer", None) is not appcontainer:
                return original_get_command_line(**kwargs)
            # The bootstrap pipe is explicitly inherited through HANDLE_LIST.
            # Omitting parent_pid makes spawn_main duplicate that handle from
            # its own process instead of opening the SYSTEM sidecar process.
            sandbox_kwargs = dict(kwargs)
            sandbox_kwargs.pop("parent_pid", None)
            return original_get_command_line(**sandbox_kwargs)

        spawn_module._winapi = _WinApiProxy(original_winapi, appcontainer)
        spawn_module.spawn.get_command_line = appcontainer_command_line
        _SPAWN_LOCAL.appcontainer = appcontainer
        try:
            popen = spawn_module.Popen(process_obj)
            process_obj._fmu_appcontainer = appcontainer
            return popen
        except BaseException:
            appcontainer.close()
            raise
        finally:
            _SPAWN_LOCAL.appcontainer = None
            spawn_module._winapi = original_winapi
            spawn_module.spawn.get_command_line = original_get_command_line


class WindowsAppContainerProcess(_SPAWN_CONTEXT.Process):
    """SpawnProcess variant that always uses a per-worker AppContainer."""

    def __init__(self, *args: Any, worker_directory: str | Path, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._fmu_worker_directory = str(worker_directory)
        self._fmu_appcontainer: WindowsAppContainer | None = None

    @staticmethod
    def _Popen(process_obj: Any) -> Any:
        return _appcontainer_popen(process_obj)
