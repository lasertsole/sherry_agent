"""Windows sandbox backend: AppContainer + restricted token + Job Object.

``wrap()`` cannot change process-creation attributes, so the backend returns a
small launcher command instead (``python -c <decoded launcher>``). The launcher
is the only place that touches Win32, and it applies three layers in order,
each failing open on its own:

1. **AppContainer** — a per-user profile gives the command a package SID of its
   own: it can write only the launcher payload's ``writable_roots`` (plus the
   shell / interpreter directories, read-only), reads nothing else of the user's,
   and reaches the network only when the ``internetClient`` capability is
   granted (i.e. when the caller did not request a network block). The DACL
   grants happen here, are inherited by children and are idempotent.
2. **Restricted token** — ``OpenProcessToken`` + ``CreateRestrictedToken`` with
   ``DISABLE_MAX_PRIVILEGE``: the child loses every privilege except
   ``SeChangeNotifyPrivilege`` (no SeDebug, no SeLoadDriver, no SeTakeOwnership),
   so the sandboxed command cannot debug another process, load a driver or take
   ownership of system files. No administrator is needed.
3. **Job Object** — process-count, memory and ``KILL_ON_JOB_CLOSE`` limits, so a
   fork bomb or an OOM is contained and killing the launcher kills the command
   and all of its descendants.
4. **stdout/stderr propagation** — the tool redirects output to a FILE handle,
   and the launcher must hand inheritable duplicates of it to the child
   (``STARTUPINFO.hStdOutput`` + ``STARTF_USESTDHANDLES`` + ``bInheritHandles``).
   Without this step a sandboxed command prints into the void.

Fail-open chain (each layer degrades independently, the command always runs):

    probe fails                  -> get_backend returns None -> unsandboxed spawn
    AppContainer profile fails   -> no file/network confinement (the note below)
    a DACL grant fails           -> that root only; the command still starts
    attribute list/create fails  -> plain restricted-token CreateProcessAsUserW
    restricted token fails       -> plain CreateProcessW (privileges intact)
    job object fails             -> token only (no resource containment)
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import sys
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from .sandbox import SandboxBackend, normalize_writable_roots
from .shell_resolver import resolve_windows_bash

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
    """The launcher's resource bounds, AppContainer switch and network block."""

    max_processes: int = _DEFAULT_MAX_PROCESSES
    max_memory_mb: int = _DEFAULT_MAX_MEMORY_MB
    block_network: bool = False
    appcontainer: bool = True


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
    ``SHERRY_WIN_SANDBOX_MAX_MEMORY_MB`` (default 4096, 64..1048576),
    ``SHERRY_WIN_SANDBOX_BLOCK_NETWORK`` (default false: the AppContainer gets
    the ``internetClient`` capability) and ``SHERRY_WIN_SANDBOX_APPCONTAINER``
    (default true; false keeps layers 2/3 only — restricted token + Job Object).
    """
    source = os.environ if env is None else env
    block = str(source.get("SHERRY_WIN_SANDBOX_BLOCK_NETWORK", "")).strip().lower()
    appcontainer = str(source.get("SHERRY_WIN_SANDBOX_APPCONTAINER", "")).strip().lower()
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
        appcontainer=appcontainer not in {"0", "false", "no", "off"},
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


def _appcontainer_name() -> str:
    """A stable per-user AppContainer profile name (<= 64 chars, no backslash)."""
    account = str(os.environ.get("USERNAME") or os.environ.get("USER") or "user").strip().lower()
    digest = hashlib.sha256(account.encode("utf-8", "replace")).hexdigest()[:12]
    return f"sherry-agent-{digest}"


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
        "appcontainer": bool(config.appcontainer),
        "appcontainer_name": _appcontainer_name(),
    }


def build_launcher_source(payload: dict[str, Any]) -> str:
    """Build the ``python -c`` launcher for one wrapped command.

    The launcher source is zlib-compressed and base64-encoded, so a command
    containing quotes, newlines or ``%`` needs no escaping at the boundary (the
    payload is JSON inside the decoded source). Compression is load-bearing:
    the plain base64 form grew past the 32 767-character Windows argv limit when
    the AppContainer layer landed (~33.5 KB), while the compressed form is ~8 KB.
    """
    body = _LAUNCHER_TEMPLATE.replace("__PAYLOAD_JSON__", json.dumps(payload))
    encoded = base64.b64encode(zlib.compress(body.encode("utf-8"), 9)).decode("ascii")
    return (
        f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{encoded}')).decode('utf-8'))"
    )


_LAUNCHER_TEMPLATE = r'''
import ctypes
import glob
import json
import os
import shutil
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
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
#: ProcThreadAttributeValue(ProcThreadAttributeSecurityCapabilities, FALSE, TRUE, FALSE)
#: — the 0x20000 "input" bit is part of the attribute identifier; plain 9 makes
#: UpdateProcThreadAttribute fail with ERROR_NOT_SUPPORTED.
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
SE_GROUP_ENABLED = 0x00000004
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
ACL_SIZE_INFORMATION_CLASS = 2
ACCESS_ALLOWED_ACE_TYPE = 0
GRANT_ACCESS = 1
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x00000003
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
DELETE = 0x00010000
FILE_DELETE_CHILD = 0x00000040
SANDBOX_WRITE_MASK = (
    FILE_GENERIC_READ
    | FILE_GENERIC_WRITE
    | FILE_GENERIC_EXECUTE
    | DELETE
    | FILE_DELETE_CHILD
)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)
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


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", wintypes.DWORD),
        ("TrusteeForm", wintypes.DWORD),
        ("TrusteeType", wintypes.DWORD),
        ("ptstrName", ctypes.c_void_p),
    ]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", wintypes.DWORD),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TRUSTEE_W),
    ]


class ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", wintypes.BYTE),
        ("AceFlags", wintypes.BYTE),
        ("AceSize", wintypes.WORD),
    ]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [
        ("Header", ACE_HEADER),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
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
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p
kernel32.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
advapi32.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
]
advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
advapi32.SetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
advapi32.SetEntriesInAclW.argtypes = [
    wintypes.ULONG,
    ctypes.POINTER(EXPLICIT_ACCESS_W),
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
advapi32.SetEntriesInAclW.restype = wintypes.DWORD
advapi32.GetAclInformation.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_int,
]
advapi32.GetAclInformation.restype = wintypes.BOOL
advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
advapi32.GetAce.restype = wintypes.BOOL
advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
advapi32.EqualSid.restype = wintypes.BOOL
userenv.CreateAppContainerProfile.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
]
userenv.CreateAppContainerProfile.restype = ctypes.c_long
userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_void_p),
]
userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
capability_api = None
for dll_name in ("kernelbase", "advapi32"):
    try:
        candidate = ctypes.WinDLL(dll_name, use_last_error=True)
        getattr(candidate, "DeriveCapabilitySidsFromName")
    except (AttributeError, OSError):
        continue
    candidate.DeriveCapabilitySidsFromName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(wintypes.DWORD),
    ]
    candidate.DeriveCapabilitySidsFromName.restype = wintypes.BOOL
    capability_api = candidate
    break


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


def appcontainer_sid(name):
    """Create (once) or derive this user's AppContainer profile SID.

    Create FIRST, derive only when the profile already exists: deriving a name
    that has no profile returns a SID, but a process created from it fails with
    ERROR_INVALID_PARAMETER because the profile's directories do not exist.
    """
    sid = ctypes.c_void_p()
    result = userenv.CreateAppContainerProfile(
        name, name, "Sherry agent sandbox", None, 0, ctypes.byref(sid)
    )
    if result == 0 and sid:
        return sid
    derived = ctypes.c_void_p()
    result = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(derived))
    if result == 0 and derived:
        return derived
    note(
        "AppContainer profile unavailable (hr=0x%08X); file/network confinement is off"
        % (result & 0xFFFFFFFF)
    )
    return None


def capability_sid(name):
    """A capability SID derived by name (CreateWellKnownSid cannot build these)."""
    if capability_api is None:
        return None
    group_sids = ctypes.POINTER(ctypes.c_void_p)()
    group_count = wintypes.DWORD()
    cap_sids = ctypes.POINTER(ctypes.c_void_p)()
    cap_count = wintypes.DWORD()
    if not capability_api.DeriveCapabilitySidsFromName(
        name,
        ctypes.byref(group_sids),
        ctypes.byref(group_count),
        ctypes.byref(cap_sids),
        ctypes.byref(cap_count),
    ):
        return None
    try:
        for index in range(group_count.value):
            kernel32.LocalFree(ctypes.cast(group_sids[index], ctypes.c_void_p))
        if group_sids:
            kernel32.LocalFree(ctypes.cast(group_sids, ctypes.c_void_p))
        if cap_count.value < 1:
            return None
        return cap_sids[0]
    finally:
        if cap_sids:
            kernel32.LocalFree(ctypes.cast(cap_sids, ctypes.c_void_p))


def acl_grants(acl, sid, mask):
    """Whether an existing ACE already grants `mask` to `sid` (idempotency)."""
    info = ACL_SIZE_INFORMATION()
    if not advapi32.GetAclInformation(
        acl, ctypes.byref(info), ctypes.sizeof(info), ACL_SIZE_INFORMATION_CLASS
    ):
        return False
    for index in range(info.AceCount):
        ace = ctypes.c_void_p()
        if not advapi32.GetAce(acl, index, ctypes.byref(ace)):
            continue
        header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
        if header.AceType != ACCESS_ALLOWED_ACE_TYPE:
            continue
        allowed = ctypes.cast(ace, ctypes.POINTER(ACCESS_ALLOWED_ACE)).contents
        if (allowed.Mask & mask) != mask:
            continue
        sid_pointer = ctypes.c_void_p(ace.value + ACCESS_ALLOWED_ACE.SidStart.offset)
        if advapi32.EqualSid(sid_pointer, sid):
            return True
    return False


def grant_path(path, sid, mask):
    """Add one inheritable allow ACE for the package SID; no-op when present."""
    if not path or not os.path.exists(path):
        return True
    if system_path(path):
        return True
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetNamedSecurityInfoW(
        path,
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        note("GetNamedSecurityInfo failed for %s (error %d)" % (path, status))
        return False
    try:
        if dacl and acl_grants(dacl, sid, mask):
            return True
        entry = EXPLICIT_ACCESS_W()
        entry.grfAccessPermissions = mask
        entry.grfAccessMode = GRANT_ACCESS
        entry.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
        entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
        entry.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
        entry.Trustee.ptstrName = sid
        new_dacl = ctypes.c_void_p()
        if advapi32.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(new_dacl)) != 0:
            note("SetEntriesInAcl failed for %s" % path)
            return False
        try:
            status = advapi32.SetNamedSecurityInfoW(
                path,
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                None,
                None,
                new_dacl,
                None,
            )
        finally:
            kernel32.LocalFree(new_dacl)
        if status != 0:
            note("SetNamedSecurityInfo failed for %s (error %d)" % (path, status))
            return False
        return True
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)


def venv_home(executable):
    """The base-interpreter directory a venv launcher re-executes (pyvenv.cfg)."""
    config_path = os.path.join(os.path.dirname(os.path.dirname(executable)), "pyvenv.cfg")
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, _, value = line.partition("=")
                if key.strip().lower() == "home" and value.strip():
                    return value.strip()
    except OSError:
        return None
    return None


def executable_roots(cmd):
    """Paths the command's own executable needs READ+EXECUTE on.

    A venv's ``python.exe`` is a launcher that re-executes the base install
    named in ``pyvenv.cfg``; the base tree is outside every writable root, so
    it must be granted too or the child dies in the launcher with "did not
    find executable". Only the base interpreter's own pieces are listed —
    ``Lib``/``DLLs`` plus the top-level executables/DLLs — never the whole
    install (its ``Doc``/``tcl`` trees are not on any import path and the
    propagation walk over them is what makes a recursive grant slow).
    """
    if not cmd:
        return []
    executable = str(cmd[0])
    if not os.path.isabs(executable):
        executable = shutil.which(executable) or executable
    parent = os.path.dirname(executable)
    roots = [executable, parent]
    if os.path.basename(parent).lower() in {"bin", "scripts", "sbin"}:
        roots.append(os.path.dirname(parent))
    home = venv_home(executable)
    if home:
        roots.extend(
            [
                os.path.join(home, "python.exe"),
                os.path.join(home, "Lib"),
                os.path.join(home, "DLLs"),
            ]
        )
        roots.extend(glob.glob(os.path.join(home, "python*.dll")))
        roots.extend(glob.glob(os.path.join(home, "vcruntime*.dll")))
    return [root for root in roots if root]


def system_path(path):
    """Whether a path lives under a system root (ALREADY readable to AppContainers).

    %SystemRoot% and %ProgramFiles% carry ``ALL APPLICATION PACKAGES``
    read+execute ACEs; adding our own ACE there needs administrator rights and
    would fail (error 5) on every sandboxed command, so those paths are
    skipped instead of warned about.
    """
    normalized = os.path.normcase(os.path.abspath(path))
    for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(key)
        if not base:
            continue
        base = os.path.normcase(os.path.abspath(base))
        if normalized == base or normalized.startswith(base + os.sep):
            return True
    return False


def build_security_capabilities(sid, allow_network):
    """(caps, keep-alive objects): internetClient only when the network is allowed."""
    capabilities = SECURITY_CAPABILITIES()
    capabilities.AppContainerSid = sid
    capabilities.Capabilities = None
    capabilities.CapabilityCount = 0
    capabilities.Reserved = 0
    if not allow_network:
        return capabilities, ()
    capability = capability_sid("internetClient")
    if capability is None:
        note("internetClient capability unavailable; the sandboxed command has no network")
        return capabilities, ()
    array = (SID_AND_ATTRIBUTES * 1)()
    array[0].Sid = ctypes.cast(capability, ctypes.c_void_p)
    array[0].Attributes = SE_GROUP_ENABLED
    capabilities.Capabilities = ctypes.cast(array, ctypes.POINTER(SID_AND_ATTRIBUTES))
    capabilities.CapabilityCount = 1
    return capabilities, (capability, array)


def attribute_list(capabilities):
    """(attribute-list handle, keep-alive buffer) or (None, None)."""
    size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if size.value <= 0:
        return None, None
    buffer = ctypes.create_string_buffer(size.value)
    handle = ctypes.cast(buffer, ctypes.c_void_p)
    if not kernel32.InitializeProcThreadAttributeList(handle, 1, 0, ctypes.byref(size)):
        return None, None
    if not kernel32.UpdateProcThreadAttribute(
        handle,
        0,
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        ctypes.byref(capabilities),
        ctypes.sizeof(capabilities),
        None,
        None,
    ):
        kernel32.DeleteProcThreadAttributeList(handle)
        return None, None
    return handle, buffer


def build_command_line(cmd):
    """Serialize argv for CreateProcess.

    ``cmd.exe /c <command>`` needs the command appended RAW: list2cmdline
    escapes inner double quotes with backslashes, which cmd.exe does not
    understand (``echo x > "C:\\path\\x"`` became a syntax error). Every other
    argv keeps the standard CRT quoting.
    """
    parts = [str(part) for part in cmd]
    if (
        len(parts) == 3
        and os.path.basename(parts[0]).lower() in {"cmd.exe", "cmd"}
        and parts[1].lower() in {"/c", "/k"}
    ):
        return subprocess.list2cmdline(parts[:2]) + " " + parts[2]
    return subprocess.list2cmdline(parts)


def create_process(token, command_line, startup_info, flags, extended):
    """CreateProcess(AsUser) returning (ok, PROCESS_INFORMATION)."""
    buffer = ctypes.create_unicode_buffer(command_line)
    process_info = PROCESS_INFORMATION()
    startup_pointer = (
        ctypes.cast(ctypes.byref(startup_info), ctypes.POINTER(STARTUPINFOW))
        if extended
        else ctypes.byref(startup_info)
    )
    if token is not None:
        ok = advapi32.CreateProcessAsUserW(
            token,
            None,
            buffer,
            None,
            None,
            True,
            flags,
            None,
            None,
            startup_pointer,
            ctypes.byref(process_info),
        )
    else:
        # CreateProcessW has no token parameter: (app, cmdline, ...) is 10 args.
        ok = kernel32.CreateProcessW(
            None,
            buffer,
            None,
            None,
            True,
            flags,
            None,
            None,
            startup_pointer,
            ctypes.byref(process_info),
        )
    return bool(ok), process_info


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

# --- layer 2: AppContainer (file/network confinement) ---
profile_sid = None
if PAYLOAD.get("appcontainer"):
    profile_sid = appcontainer_sid(str(PAYLOAD.get("appcontainer_name") or "sherry-agent"))
    if profile_sid is not None:
        for root in PAYLOAD.get("roots") or []:
            if not grant_path(root, profile_sid, SANDBOX_WRITE_MASK):
                note("write access could not be granted on %s" % root)
        # The command's shell AND this launcher's own interpreter (a venv's
        # python may be invoked from inside a cmd command through PATH).
        for candidate in [sys.executable] + [str(part) for part in (PAYLOAD.get("cmd") or [])[:1]]:
            for root in executable_roots([candidate]):
                grant_path(root, profile_sid, FILE_GENERIC_READ | FILE_GENERIC_EXECUTE)

# --- create the child (suspended, so the job binds before it can spawn) ---
command_line = build_command_line(PAYLOAD["cmd"])
create_flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | CREATE_SUSPENDED
process_info = PROCESS_INFORMATION()
created = False

if profile_sid is not None:
    try:
        capabilities, keep_alive = build_security_capabilities(
            profile_sid, not PAYLOAD.get("block_network")
        )
        attribute_handle, list_buffer = attribute_list(capabilities)
        if attribute_handle is not None:
            extended_startup = STARTUPINFOEXW()
            extended_startup.StartupInfo = startup
            # EXTENDED_STARTUPINFO_PRESENT requires cb to cover the EXTENDED
            # struct (a plain STARTUPINFO cb fails with ERROR_INVALID_PARAMETER).
            extended_startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            extended_startup.lpAttributeList = attribute_handle
            created, process_info = create_process(
                token,
                command_line,
                extended_startup,
                create_flags | EXTENDED_STARTUPINFO_PRESENT,
                True,
            )
            kernel32.DeleteProcThreadAttributeList(attribute_handle)
            if not created:
                note(
                    "AppContainer process creation failed (%d); falling back"
                    % ctypes.get_last_error()
                )
        else:
            note("AppContainer attribute list unavailable; falling back")
    except Exception as exc:  # noqa: BLE001 - layer 2 must never block the command
        note("AppContainer setup failed (%s); falling back" % exc)
        created = False

if not created:
    if token is not None:
        created, process_info = create_process(token, command_line, startup, create_flags, False)
        if not created:
            note("CreateProcessAsUserW failed (%d); falling back" % ctypes.get_last_error())
    if not created:
        created, process_info = create_process(None, command_line, startup, create_flags, False)
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
    """Windows backend: an AppContainer + restricted-token + Job-Object launcher."""

    def __init__(self) -> None:
        self._probe_cache: bool | None = None

    def probe(self) -> bool:
        """True when a restricted token and a Job Object can both be created.

        AppContainer is optional (an enterprise policy can forbid the profile):
        its absence degrades to layers 1 + 3, so it does not fail the probe.
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
        roots = normalize_writable_roots(writable_roots)
        config = read_win_config()
        block_network = config.block_network and not allow_network
        payload = _launcher_payload(cmd, roots, config, block_network=block_network)
        # The AppContainer child cannot write the user's %TEMP%; point the
        # command's temp at a granted root so tools that need scratch space
        # keep working without widening the write surface.
        child_env = dict(env)
        temp_dir = next(
            (root for root in roots if Path(root).name.lower() == "temp"),
            roots[0] if roots else None,
        )
        if temp_dir is not None:
            child_env["TEMP"] = str(temp_dir)
            child_env["TMP"] = str(temp_dir)
        return [sys.executable, "-c", build_launcher_source(payload)], child_env

    def sandbox_shell(self, cmd: str) -> list[str]:
        """The sandboxed shell: cmd.exe under AppContainer, Git Bash otherwise.

        Git Bash (MSYS2) cannot start inside an AppContainer — its runtime
        needs global named objects that an AppContainer's namespace does not
        expose, so the process dies with STATUS_DLL_INIT_FAILED before its
        first instruction. cmd.exe runs in both modes, so the confined shell
        is cmd.exe; without AppContainer the historical Git Bash preference
        stays (the tool promises a POSIX shell there).
        """
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        if read_win_config().appcontainer:
            return [comspec, "/c", cmd]
        bash_exe = resolve_windows_bash()
        if bash_exe:
            return [bash_exe, "-c", cmd]
        return [comspec, "/c", cmd]
