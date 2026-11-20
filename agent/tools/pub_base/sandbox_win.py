"""Windows sandbox backend: restricted token + Job Object behind a launcher.

``wrap()`` cannot change process-creation attributes, so the backend returns a
small launcher command instead (``python -c <decoded launcher>``). The launcher
is the only place that touches Win32, and it does three things in order, each
failing open on its own:

1. **Restricted token** — ``OpenProcessToken`` + ``CreateRestrictedToken`` with
   ``DISABLE_MAX_PRIVILEGE``: the child loses every privilege (no SeDebug, no
   SeLoadDriver, no SeTakeOwnership), so the sandboxed command cannot debug
   another process, load a driver or take ownership of system files. Creating a
   restricted version of the caller's own token needs no administrator.
2. **Job Object** — process-count, memory and ``KILL_ON_JOB_CLOSE`` limits, so a
   fork bomb or an OOM is contained and killing the launcher kills the command
   and all of its descendants.
3. **stdout/stderr propagation** — the tool redirects output to a FILE handle,
   and the launcher must hand inheritable duplicates of it to the child
   (``STARTUPINFO.hStdOutput`` + ``STARTF_USESTDHANDLES`` + ``bInheritHandles``).
   Without this step a sandboxed command prints into the void.

AppContainer (filesystem / network confinement) is NOT implemented yet: the
plan's remaining layer. When a caller asks for network blocking the launcher
logs that the layer is unavailable and keeps the network (fail-open); the write
surface is still bounded by the tool-level path guard and the HITL gate.

Fail-open chain (each layer degrades independently, the command always runs):

    probe fails            -> get_backend returns None -> unsandboxed spawn
    restricted token fails -> plain CreateProcessW (privileges intact)
    job object fails       -> token only (no resource containment)
    create-as-user fails   -> CreateProcessW (still job-bound)
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from .sandbox import SandboxBackend, normalize_writable_roots

__all__ = [
    "WinSandboxBackend",
    "WinSandboxConfig",
    "build_launcher_source",
    "read_win_config",
]

#: Default per-cell resource bounds (env-overridable, see :func:`read_win_config`).
_DEFAULT_MAX_PROCESSES = 50
_DEFAULT_MAX_MEMORY_MB = 4096
_MAX_PROCESSES_CEILING = 10_000
_MEMORY_MB_CEILING = 1_048_576

#: Win32 constants used by the in-process probe (the launcher has its own copy:
#: it must stay self-contained, no project imports inside the child).
_TOKEN_QUERY = 0x0008
_TOKEN_DUPLICATE = 0x0002
_TOKEN_ASSIGN_PRIMARY = 0x0001
_DISABLE_MAX_PRIVILEGE = 0x0001

#: One-shot warnings for a requested-but-unavailable capability.
_WARNED_NETWORK_BLOCK = False


@dataclass(frozen=True)
class WinSandboxConfig:
    """The launcher's resource bounds and optional network block."""

    max_processes: int = _DEFAULT_MAX_PROCESSES
    max_memory_mb: int = _DEFAULT_MAX_MEMORY_MB
    block_network: bool = False


def _read_int(source: dict[str, str], name: str, default: int, *, low: int, high: int) -> int:
    """Read an int from ``source``, clamping and falling back instead of raising."""
    raw = str(source.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("sandbox_win: {}={!r} is not an integer; using {}", name, raw, default)
        return default
    return max(low, min(high, value))


def read_win_config(env: dict[str, str] | None = None) -> WinSandboxConfig:
    """Read the Windows sandbox limits from the environment.

    ``SHERRY_WIN_SANDBOX_MAX_PROCESSES`` (default 50, 1..10000),
    ``SHERRY_WIN_SANDBOX_MAX_MEMORY_MB`` (default 4096, 64..1048576) and
    ``SHERRY_WIN_SANDBOX_BLOCK_NETWORK`` (default false; needs the AppContainer
    layer, which is not implemented yet — the request is logged and ignored).
    """
    source = os.environ if env is None else env
    block = str(source.get("SHERRY_WIN_SANDBOX_BLOCK_NETWORK", "")).strip().lower()
    return WinSandboxConfig(
        max_processes=_read_int(
            source,
            "SHERRY_WIN_SANDBOX_MAX_PROCESSES",
            _DEFAULT_MAX_PROCESSES,
            low=1,
            high=_MAX_PROCESSES_CEILING,
        ),
        max_memory_mb=_read_int(
            source,
            "SHERRY_WIN_SANDBOX_MAX_MEMORY_MB",
            _DEFAULT_MAX_MEMORY_MB,
            low=64,
            high=_MEMORY_MB_CEILING,
        ),
        block_network=block in {"1", "true", "yes", "on"},
    )


def _load_win32() -> tuple[Any, Any] | None:
    """Load kernel32/advapi32 with the prototypes the probe needs, or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.CreateRestrictedToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.CreateRestrictedToken.restype = wintypes.BOOL
        return kernel32, advapi32
    except (AttributeError, OSError):
        logger.debug("sandbox_win: kernel32/advapi32 unavailable", exc_info=True)
        return None


def _launcher_payload(
    cmd: Sequence[str],
    writable_roots: Sequence[Path],
    config: WinSandboxConfig,
    *,
    block_network: bool | None = None,
) -> dict[str, Any]:
    """The JSON the launcher reads: the command, the roots and the limits."""
    return {
        "cmd": [str(part) for part in cmd],
        "roots": [str(Path(root)) for root in writable_roots],
        "max_processes": int(config.max_processes),
        "max_memory_mb": int(config.max_memory_mb),
        "block_network": bool(config.block_network if block_network is None else block_network),
    }


def build_launcher_source(payload: dict[str, Any]) -> str:
    """Build the ``python -c`` launcher for one wrapped command.

    The whole launcher is base64-encoded, so a command containing quotes,
    newlines or ``%`` needs no escaping at the boundary (the payload is JSON
    inside the decoded source). The result is a few kilobytes — far below the
    32 767-character Windows argv limit.
    """
    body = _LAUNCHER_TEMPLATE.replace("__PAYLOAD_JSON__", json.dumps(payload))
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return f"import base64;exec(base64.b64decode('{encoded}').decode('utf-8'))"


_LAUNCHER_TEMPLATE = r'''
import ctypes
import json
import subprocess
import sys

import ctypes.wintypes as wintypes

PAYLOAD = json.loads(r"""__PAYLOAD_JSON__""")

TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002
TOKEN_ASSIGN_PRIMARY = 0x0001
DISABLE_MAX_PRIVILEGE = 0x0001
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_SUSPENDED = 0x00000004
STARTF_USESTDHANDLES = 0x00000100
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
DUPLICATE_SAME_ACCESS = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
INFINITE = 0xFFFFFFFF

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t


class STARTUPINFOW(ctypes.Structure):
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
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ULONG_PTR),
        ("MaximumWorkingSetSize", ULONG_PTR),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ULONG_PTR),
        ("JobMemoryLimit", ULONG_PTR),
        ("PeakProcessMemoryUsed", ULONG_PTR),
        ("PeakJobMemoryUsed", ULONG_PTR),
    ]


kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
kernel32.GetStdHandle.restype = wintypes.HANDLE
kernel32.DuplicateHandle.argtypes = [
    wintypes.HANDLE,
    wintypes.HANDLE,
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
kernel32.DuplicateHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.GetLastError.restype = wintypes.DWORD
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.CreateRestrictedToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.CreateRestrictedToken.restype = wintypes.BOOL
advapi32.CreateProcessAsUserW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
advapi32.CreateProcessAsUserW.restype = wintypes.BOOL


def note(message):
    print("[sandbox] " + message, file=sys.stderr, flush=True)


def duplicate_inheritable(source):
    target = wintypes.HANDLE()
    if not source:
        return None
    ok = kernel32.DuplicateHandle(
        kernel32.GetCurrentProcess(),
        source,
        kernel32.GetCurrentProcess(),
        ctypes.byref(target),
        0,
        True,
        DUPLICATE_SAME_ACCESS,
    )
    return target if ok else None


stdout_handle = duplicate_inheritable(kernel32.GetStdHandle(STD_OUTPUT_HANDLE))
stderr_handle = duplicate_inheritable(kernel32.GetStdHandle(STD_ERROR_HANDLE))
stdin_handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)

startup = STARTUPINFOW()
startup.cb = ctypes.sizeof(STARTUPINFOW)
startup.dwFlags = STARTF_USESTDHANDLES
startup.hStdInput = stdin_handle
startup.hStdOutput = stdout_handle or kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
startup.hStdError = stderr_handle or startup.hStdOutput

# --- layer 1: restricted token (privileges removed) ---
token = None
current_token = wintypes.HANDLE()
if advapi32.OpenProcessToken(
    kernel32.GetCurrentProcess(),
    TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY,
    ctypes.byref(current_token),
):
    restricted_token = wintypes.HANDLE()
    if advapi32.CreateRestrictedToken(
        current_token,
        DISABLE_MAX_PRIVILEGE,
        0,
        None,
        0,
        None,
        0,
        None,
        ctypes.byref(restricted_token),
    ):
        token = restricted_token
    else:
        note("CreateRestrictedToken failed (%d); running with full privileges" % ctypes.get_last_error())
    kernel32.CloseHandle(current_token)
else:
    note("OpenProcessToken failed (%d); running with full privileges" % ctypes.get_last_error())

# --- layer 3: Job Object (process/memory caps + kill-on-close) ---
job = kernel32.CreateJobObjectW(None, None)
if job:
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    limits.BasicLimitInformation.ActiveProcessLimit = int(PAYLOAD["max_processes"])
    limits.JobMemoryLimit = int(PAYLOAD["max_memory_mb"]) * 1024 * 1024
    if not kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        note("SetInformationJobObject failed (%d); resource limits are not applied" % ctypes.get_last_error())
else:
    note("CreateJobObject failed (%d); no resource limits" % ctypes.get_last_error())

if PAYLOAD.get("block_network"):
    note("network blocking requires the AppContainer layer (not implemented); network stays available")

# --- create the child (suspended, so the job binds before it can spawn) ---
command_line = subprocess.list2cmdline([str(part) for part in PAYLOAD["cmd"]])
create_flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | CREATE_SUSPENDED
process_info = PROCESS_INFORMATION()
created = False
if token is not None:
    buffer = ctypes.create_unicode_buffer(command_line)
    created = bool(
        advapi32.CreateProcessAsUserW(
            token,
            None,
            buffer,
            None,
            None,
            True,
            create_flags,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(process_info),
        )
    )
    if not created:
        note("CreateProcessAsUserW failed (%d); falling back" % ctypes.get_last_error())
if not created:
    buffer = ctypes.create_unicode_buffer(command_line)
    created = bool(
        kernel32.CreateProcessW(
            None,
            buffer,
            None,
            None,
            True,
            create_flags,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(process_info),
        )
    )
    if not created:
        note("CreateProcessW failed (%d); the command did not start" % ctypes.get_last_error())
        sys.exit(1)

if job:
    if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
        note("AssignProcessToJobObject failed (%d)" % ctypes.get_last_error())
kernel32.ResumeThread(process_info.hThread)

kernel32.WaitForSingleObject(process_info.hProcess, INFINITE)
exit_code = wintypes.DWORD()
kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code))
kernel32.CloseHandle(process_info.hThread)
kernel32.CloseHandle(process_info.hProcess)
if token is not None:
    kernel32.CloseHandle(token)
if job:
    kernel32.CloseHandle(job)
sys.exit(int(exit_code.value))
'''


class WinSandboxBackend(SandboxBackend):
    """Windows backend: a restricted-token + Job-Object launcher per command."""

    def __init__(self) -> None:
        self._probe_cache: bool | None = None

    def probe(self) -> bool:
        """True when a restricted token and a Job Object can both be created.

        AppContainer is optional (not implemented yet), so its absence does not
        fail the probe: layers 1 and 3 already remove privileges and bound the
        process tree.
        """
        if self._probe_cache is not None:
            return self._probe_cache
        win32 = _load_win32()
        if win32 is None:
            self._probe_cache = False
            return False
        kernel32, advapi32 = win32
        try:
            import ctypes.wintypes as wintypes

            current_token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(),
                _TOKEN_QUERY | _TOKEN_DUPLICATE,
                ctypes.byref(current_token),
            ):
                self._probe_cache = False
                return False
            restricted = wintypes.HANDLE()
            created = advapi32.CreateRestrictedToken(
                current_token,
                _DISABLE_MAX_PRIVILEGE,
                0,
                None,
                0,
                None,
                0,
                None,
                ctypes.byref(restricted),
            )
            kernel32.CloseHandle(current_token)
            if not created or not restricted:
                self._probe_cache = False
                return False
            kernel32.CloseHandle(restricted)
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                self._probe_cache = False
                return False
            kernel32.CloseHandle(job)
            self._probe_cache = True
        except Exception:  # noqa: BLE001 - a probe must never raise
            logger.debug("sandbox_win: probe failed", exc_info=True)
            self._probe_cache = False
        return self._probe_cache

    def wrap(
        self,
        cmd: list[str],
        env: dict,
        writable_roots: Sequence[Path],
        *,
        allow_network: bool = False,
    ) -> tuple[list[str], dict]:
        """Return the launcher command; the launcher applies the isolation."""
        global _WARNED_NETWORK_BLOCK
        roots = normalize_writable_roots(writable_roots)
        config = read_win_config()
        block_network = config.block_network and not allow_network
        if block_network and not _WARNED_NETWORK_BLOCK:
            _WARNED_NETWORK_BLOCK = True
            logger.warning(
                "sandbox_win: SHERRY_WIN_SANDBOX_BLOCK_NETWORK is set but the "
                "AppContainer layer is not implemented; the sandbox keeps the "
                "network (privileges are still removed and the job still bounds the process tree)"
            )
        payload = _launcher_payload(cmd, roots, config, block_network=block_network)
        return [sys.executable, "-c", build_launcher_source(payload)], env
