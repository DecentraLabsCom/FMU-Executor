"""Operating-system resource limits for untrusted native FMU workers."""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from pathlib import Path
from typing import Any

from . import config


class WorkerIsolationError(RuntimeError):
    """The platform could not apply the configured worker limits."""


def bind_worker_to_parent(parent_pid: int) -> None:
    """Kill Linux workers if their API supervisor exits, including startup races."""
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(1, 9, None, 0, 0) != 0:  # PR_SET_PDEATHSIG / SIGKILL
        raise WorkerIsolationError("Unable to bind FMU worker lifetime to its supervisor")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), 9)
        os._exit(1)


def worker_process_is_alive(pid: int) -> bool:
    """Check worker lifetime without sending a signal on Windows."""
    if pid < 1:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (OSError, ValueError):
            return False

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied means the process still exists
    try:
        exit_code = ctypes.c_uint32()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        kernel.CloseHandle(handle)


def worker_isolation_capabilities() -> dict[str, Any]:
    """Describe enforced worker controls without implying unsupported sandboxes."""
    if sys.platform.startswith("linux"):
        return {
            "mechanisms": ["rlimit", "landlock", "seccomp"],
            "cpuLimit": True,
            "memoryLimit": True,
            "childProcessLimit": True,
            "networkDisabled": True,
            "writesLimitedToWorkDirectory": True,
            "temporaryFileLimitPerFile": True,
            "temporaryStorageAggregateLimit": False,
            "readAccessRestricted": True,
            "lowPrivilegeIdentity": False,
            "failClosed": True,
        }
    if os.name == "nt":
        return {
            "mechanisms": ["windows-app-container", "windows-job-object", "private-working-directory"],
            "cpuLimit": True,
            "memoryLimit": True,
            "childProcessLimit": True,
            "networkDisabled": True,
            "writesLimitedToWorkDirectory": False,
            "writesLimitedToPrivateLocations": True,
            "isolatedProfileStorage": True,
            "temporaryFileLimitPerFile": False,
            "temporaryStorageAggregateLimit": False,
            "readAccessRestricted": True,
            "lowPrivilegeIdentity": False,
            "appContainerToken": True,
            "failClosed": True,
        }
    return {
        "mechanisms": ["rlimit"],
        "cpuLimit": True,
        "memoryLimit": True,
        "childProcessLimit": False,
        "networkDisabled": False,
        "writesLimitedToWorkDirectory": False,
        "temporaryFileLimitPerFile": True,
        "temporaryStorageAggregateLimit": False,
        "readAccessRestricted": False,
        "lowPrivilegeIdentity": False,
        "failClosed": False,
    }


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def apply_worker_limits(
    policy: dict[str, Any] | None = None,
    worker_directory: str | None = None,
    fmu_path: str | None = None,
) -> None:
    """Apply platform worker limits before loading untrusted native FMU code."""
    if os.name == "nt":
        return

    try:
        import resource

        policy = policy or {}
        cpu_seconds = min(config.WORKER_CPU_SECONDS, int(policy.get("maxCpuSeconds", config.WORKER_CPU_SECONDS)))
        memory_bytes = min(
            config.WORKER_MEMORY_BYTES,
            int(policy.get("maxMemoryMiB", config.WORKER_MEMORY_BYTES // (1024 * 1024))) * 1024 * 1024,
        )
        file_bytes = config.WORKER_FILE_BYTES
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if sys.platform.startswith("linux"):
            if not worker_directory:
                raise WorkerIsolationError("Linux FMU worker has no private work directory")
            if not fmu_path:
                raise WorkerIsolationError("Linux FMU worker has no immutable FMU path")
            _install_linux_landlock(worker_directory, fmu_path)
            _install_linux_worker_filter()
    except (ImportError, OSError, ValueError) as exc:
        raise WorkerIsolationError("Unable to apply FMU worker resource limits") from exc


def _install_linux_landlock(worker_directory: str, fmu_path: str) -> None:
    """Whitelist runtime reads and limit FMU writes to the private worker directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.syscall.argtypes = [ctypes.c_long]
    syscall_base = 444  # Linux generic syscall numbers on x86_64 and aarch64
    version = libc.syscall(syscall_base, None, 0, 1)
    if version < 1:
        raise WorkerIsolationError("Linux Landlock filesystem isolation is unavailable")

    execute = 1 << 0
    write_file = 1 << 1
    read_file = 1 << 2
    read_dir = 1 << 3
    remove_dir = 1 << 4
    remove_file = 1 << 5
    make_bits = sum(1 << bit for bit in range(6, 13))
    refer = 1 << 13 if version >= 2 else 0
    truncate = 1 << 14 if version >= 3 else 0
    handled = execute | write_file | read_file | read_dir | remove_dir | remove_file | make_bits | refer | truncate
    traverse_access = execute
    read_directory_access = execute | read_file | read_dir
    worker_access = handled
    read_file_access = read_file
    ruleset_attr = _LandlockRulesetAttr(handled)
    ruleset_fd = int(libc.syscall(syscall_base, ctypes.byref(ruleset_attr), ctypes.sizeof(ruleset_attr), 0))
    if ruleset_fd < 0:
        raise WorkerIsolationError("Unable to create the Linux worker filesystem ruleset")
    opened_fds: list[int] = []
    try:
        path_flag = getattr(os, "O_PATH", os.O_RDONLY)
        cloexec = getattr(os, "O_CLOEXEC", 0)

        def add_path_rule(path: str | Path, allowed_access: int, *, required: bool = False) -> None:
            candidate = Path(path)
            try:
                candidate = candidate.resolve(strict=True)
                descriptor = os.open(str(candidate), path_flag | cloexec)
            except OSError:
                if required:
                    raise WorkerIsolationError("A required FMU worker runtime path is unavailable")
                return
            opened_fds.append(descriptor)
            rule = _LandlockPathBeneathAttr(allowed_access, descriptor)
            if libc.syscall(syscall_base + 1, ruleset_fd, 1, ctypes.byref(rule), 0) < 0:
                raise WorkerIsolationError("Unable to add an FMU worker filesystem rule")

        root_fd = os.open("/", path_flag | cloexec)
        opened_fds.append(root_fd)
        root_rule = _LandlockPathBeneathAttr(traverse_access, root_fd)
        if libc.syscall(syscall_base + 1, ruleset_fd, 1, ctypes.byref(root_rule), 0) < 0:
            raise WorkerIsolationError("Unable to add the FMU worker traversal rule")

        app_root = Path(__file__).resolve().parent.parent
        runtime_directories = {
            Path(sys.prefix),
            Path(sys.base_prefix),
            app_root,
            Path("/lib"),
            Path("/lib64"),
            Path("/usr/lib"),
            Path("/usr/lib64"),
            Path("/usr/share/zoneinfo"),
            Path("/usr/share/locale"),
        }
        for runtime_directory in runtime_directories:
            add_path_rule(runtime_directory, read_directory_access, required=runtime_directory in {Path(sys.prefix), app_root})
        add_path_rule(worker_directory, worker_access, required=True)
        add_path_rule(fmu_path, read_file_access, required=True)

        for runtime_file in (
            "/etc/ld.so.cache", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group",
            "/etc/localtime", "/proc/self/maps", "/proc/cpuinfo", "/proc/meminfo",
            "/dev/null", "/dev/zero", "/dev/urandom", "/dev/random",
        ):
            add_path_rule(runtime_file, read_file_access)

        proc_self = Path("/proc/self")
        add_path_rule(proc_self, read_directory_access)

        if libc.prctl(38, 1, None, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise WorkerIsolationError("Unable to prevent FMU worker privilege escalation")
        if libc.syscall(syscall_base + 2, ruleset_fd, 0) < 0:
            raise WorkerIsolationError("Unable to restrict the FMU worker filesystem")
    finally:
        for descriptor in opened_fds:
            os.close(descriptor)
        os.close(ruleset_fd)


def _install_linux_worker_filter() -> None:
    """Deny network socket creation and executable child processes in Linux workers."""
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        denied = (
            41, 53, 42, 49, 50, 43, 288, 44, 45, 46, 47, 48, 54, 55,
            425, 426, 427, 59, 322, 57, 58,
        )
        clone_number, clone3_number, clone_thread_flag = 56, 435, 0x00010000
    elif machine in {"aarch64", "arm64"}:
        denied = (
            198, 199, 203, 200, 201, 202, 242, 206, 207, 208, 209, 210,
            211, 212, 425, 426, 427, 221, 281,
        )
        clone_number, clone3_number, clone_thread_flag = 220, 435, 0x00010000
    else:
        raise WorkerIsolationError("Linux worker isolation is unsupported on this architecture")

    instructions: list[_SockFilter] = [_SockFilter(0x20, 0, 0, 0)]  # seccomp_data.nr
    for syscall_number in denied:
        instructions.extend((
            _SockFilter(0x15, 0, 1, syscall_number),  # JEQ: skip the next instruction when not equal
            _SockFilter(0x06, 0, 0, 0x00050000 | 1),  # RET_ERRNO(EPERM)
        ))
    instructions.extend((
        _SockFilter(0x15, 0, 4, clone_number),
        _SockFilter(0x20, 0, 0, 16),  # seccomp_data.args[0] (clone flags)
        _SockFilter(0x45, 1, 0, clone_thread_flag),  # JSET: allow pthread clone only
        _SockFilter(0x06, 0, 0, 0x00050000 | 1),
        _SockFilter(0x06, 0, 0, 0x7FFF0000),
        _SockFilter(0x15, 0, 1, clone3_number),
        _SockFilter(0x06, 0, 0, 0x00050000 | 38),  # ENOSYS makes libc retry with clone()
        _SockFilter(0x06, 0, 0, 0x7FFF0000),  # allow everything else
    ))
    filter_array = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len(instructions), filter_array)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(38, 1, None, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise WorkerIsolationError("Unable to prevent FMU worker privilege escalation")
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:  # PR_SET_SECCOMP/FILTER
        raise WorkerIsolationError("Unable to install the FMU worker syscall filter")


class WindowsWorkerJob:
    """Windows Job Object that bounds memory, CPU time, and child processes."""

    _JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
    _JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(
        self,
        pid: int,
        policy: dict[str, Any] | None = None,
        appcontainer: Any | None = None,
    ):
        if os.name != "nt":
            raise WorkerIsolationError("Windows Job Objects are only available on Windows")
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel.SetInformationJobObject.restype = ctypes.c_int
        self._kernel.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        self._kernel.OpenProcess.restype = ctypes.c_void_p
        self._kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self._kernel.AssignProcessToJobObject.restype = ctypes.c_int
        self._kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._kernel.TerminateJobObject.restype = ctypes.c_int
        self._kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel.CloseHandle.restype = ctypes.c_int
        self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self._job = self._kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise WorkerIsolationError("Unable to create FMU worker Job Object")
        self._process = None
        self._appcontainer = appcontainer
        try:
            limits = _JobExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = (
                self._JOB_OBJECT_LIMIT_PROCESS_TIME
                | self._JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | self._JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            policy = policy or {}
            cpu_seconds = min(config.WORKER_CPU_SECONDS, int(policy.get("maxCpuSeconds", config.WORKER_CPU_SECONDS)))
            memory_bytes = min(
                config.WORKER_MEMORY_BYTES,
                int(policy.get("maxMemoryMiB", config.WORKER_MEMORY_BYTES // (1024 * 1024))) * 1024 * 1024,
            )
            limits.BasicLimitInformation.PerProcessUserTimeLimit = cpu_seconds * 10_000_000
            limits.BasicLimitInformation.ActiveProcessLimit = 1
            limits.ProcessMemoryLimit = memory_bytes
            if not self._kernel.SetInformationJobObject(
                self._job,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise WorkerIsolationError("Unable to configure FMU worker Job Object")

            self._process = self._kernel.OpenProcess(
                self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE | self._PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )
            if not self._process:
                raise WorkerIsolationError("Unable to open FMU worker process for isolation")
            if not self._kernel.AssignProcessToJobObject(self._job, self._process):
                raise WorkerIsolationError("Unable to assign FMU worker to its isolation Job Object")
        except Exception:
            self.close()
            raise

    def terminate(self, exit_code: int = 1) -> None:
        if self._job:
            self._kernel.TerminateJobObject(self._job, exit_code)

    def close(self) -> None:
        if self._process:
            self._kernel.CloseHandle(self._process)
            self._process = None
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None
        if self._appcontainer is not None:
            appcontainer = self._appcontainer
            self._appcontainer = None
            appcontainer.close()


def attach_worker_limits(process: Any, policy: dict[str, Any] | None = None) -> WindowsWorkerJob | None:
    """Attach a spawned worker to OS-level descendant/resource controls."""
    if os.name == "nt":
        try:
            appcontainer = getattr(process, "_fmu_appcontainer", None)
            if appcontainer is None:
                raise WorkerIsolationError("Windows FMU worker was not launched in an AppContainer")
            return WindowsWorkerJob(int(process.pid), policy, appcontainer)
        except WorkerIsolationError:
            raise
        except Exception as exc:
            raise WorkerIsolationError("Unable to isolate FMU worker") from exc
    return None


def terminate_worker(process: Any, job: WindowsWorkerJob | None = None) -> None:
    """Stop a worker and, on Windows, every process in its Job Object."""
    if process.pid is None:
        if job is not None:
            job.close()
        else:
            appcontainer = getattr(process, "_fmu_appcontainer", None)
            if appcontainer is not None:
                appcontainer.close()
                process._fmu_appcontainer = None
        return
    if process.is_alive():
        if job is not None:
            job.terminate()
        else:
            process.terminate()
    process.join(timeout=2)
    if process.is_alive():
        if job is not None:
            job.terminate()
        else:
            process.kill()
        process.join(timeout=2)
    if job is not None:
        job.close()
    else:
        appcontainer = getattr(process, "_fmu_appcontainer", None)
        if appcontainer is not None:
            appcontainer.close()
            process._fmu_appcontainer = None
