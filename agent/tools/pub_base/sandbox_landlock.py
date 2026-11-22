"""Linux landlock sandbox backend — the kernel-level fallback for bwrap.

bwrap needs the setuid binary present; slim containers (distroless images, the
eval harness's minimal runners) ship none, and those environments used to fall
all the way back to UNSANDBOXED. landlock is a kernel LSM (Linux 5.13+): no
external binary, no mounts — the process restricts itself through three
syscalls and every descendant inherits the restriction.

How it runs: landlock can only restrict the CALLING process (there is no
"wrap this argv" form), so ``wrap()`` returns a launcher — this interpreter in
``-I -c`` mode — whose script applies the rules and ``execvp``s the real
command. The rules travel in one env var (popped before exec, so the command
never sees them). An init failure exits 126 with a stderr message: the sandbox
fails CLOSED, exactly like a missing bwrap under ``required`` — silently
running unsandboxed is the one failure this layer must never have.

The ruleset is an ALLOW-LIST, which makes the backend stricter than bwrap's
deny-list in one direction and stronger in the other:

* READ is granted only to the system trees (``/usr /bin /sbin /lib /lib64
  /etc /opt /dev /proc /sys``) — everything else, including ``~/.ssh`` and the
  other credential directories bwrap only MASKS via regex, is denied by
  omission;
* READ+WRITE go to the caller's writable roots (project, own session tree,
  ``TEMP_DIR``) plus ``/dev/null`` (a redirect every shell script expects).

The trade-off, documented for the model-facing docs: reads outside the listed
trees fail inside the sandbox. A packaged (frozen) build is not supported —
``sys.executable`` there is the app bundle, whose entrypoint would boot the
whole server instead of the loader — and ``probe()`` reports unsupported, so
the dispatch keeps bwrap (or degrades as before).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from config.features import TOOLS_TIMEOUTS

from agent.tools.pub_base.sandbox import SandboxBackend, normalize_writable_roots

__all__ = ["LandlockBackend"]

#: landlock syscall numbers. x86_64 and aarch64 share the same table entries;
#: other machines are unsupported (the probe answers False).
_SYSCALL_CREATE_RULESET = 444
_SYSCALL_ADD_RULE = 445
_SYSCALL_RESTRICT_SELF = 446
_SUPPORTED_MACHINES = frozenset({"x86_64", "aarch64"})

_PR_SET_NO_NEW_PRIVS = 38
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

# handled_access_fs bits (uaf/fs-access rights, kernel landlock.h order).
_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
#: REFER (abi v2) lets a file be renamed/linked BETWEEN directories — without
#: it ``mv a b`` across directories fails inside the sandbox.
_REFER = 1 << 13
#: TRUNCATE (abi v3) gates open(..., O_TRUNC); without handling it, a v3+
#: kernel treats truncate as always allowed, so handling it TIGHTENS only when
#: the kernel understands it.
_TRUNCATE = 1 << 14

_READ_RIGHTS = _READ_FILE | _READ_DIR | _EXECUTE
_WRITE_RIGHTS = (
    _READ_RIGHTS
    | _WRITE_FILE
    | _REMOVE_DIR
    | _REMOVE_FILE
    | _MAKE_CHAR
    | _MAKE_DIR
    | _MAKE_REG
    | _MAKE_SOCK
    | _MAKE_FIFO
    | _MAKE_BLOCK
    | _MAKE_SYM
)

#: The read allow-list: the system trees a command needs to find its
#: interpreter, libraries and configuration. Everything else is denied.
_SYSTEM_READ_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
    "/dev",
    "/proc",
    "/sys",
)
#: Write exceptions under the read-only system trees.
_SYSTEM_WRITE_PATHS = ("/dev/null",)

#: The launcher: this interpreter, isolated mode (no PYTHONPATH, no user site,
#: no cwd on sys.path — a planted ``json.py`` must never shadow the loader's
#: imports), the loader source, then the real command after ``--``.
_LOADER_ARGUMENTS = ("-I", "-c")

_LOADER_SRC = r"""
import ctypes, json, os, platform, sys

def _fail(message):
    sys.stderr.write("landlock sandbox failed to initialize: %s\n" % message)
    raise SystemExit(126)

raw = os.environ.pop("SHERRY_LANDLOCK_SPEC", None)
if not raw:
    _fail("spec missing")
try:
    spec = json.loads(raw)
except ValueError as exc:
    _fail("bad spec: %s" % exc)

abi = int(spec.get("abi", 0))
machine = platform.machine()
numbers = {"x86_64": (444, 445, 446), "aarch64": (444, 445, 446)}.get(machine)
if numbers is None:
    _fail("unsupported machine %s" % machine)

handled = 0x1FFF  # EXECUTE..MAKE_SYM (bits 0-12)
if abi >= 2:
    handled |= 1 << 13  # REFER
if abi >= 3:
    handled |= 1 << 14  # TRUNCATE

libc = ctypes.CDLL(None, use_errno=True)

class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]

class PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]

def _syscall(number, *args):
    libc.syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(number), *args)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code) if code else "syscall failed")
    return result

try:
    attr = RulesetAttr(handled)
    # landlock_create_ruleset(attr, size, flags): flags MUST be 0 to create a
    # ruleset. LANDLOCK_CREATE_RULESET_VERSION (=1) is only valid for the ABI
    # probe (attr=NULL, size=0); passing it with a real attr makes the kernel
    # reject the call with EINVAL, which used to fail-close every landlock run.
    ruleset_fd = _syscall(numbers[0], ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset_fd <= 0:
        _fail("empty ruleset fd")
    # landlock rules never apply to a process that can gain privileges.
    libc.prctl(_LL_NO_NEW_PRIVS, 1, 0, 0, 0)

    def _add(path, access):
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except FileNotFoundError:
            # A fixed system path (e.g. /lib64) is absent on this distro's
            # layout (usrmerge). Skipping an allow-list rule for a nonexistent
            # path cannot widen access — there is nothing there to open — so
            # this stays fail-closed for every real path.
            return
        try:
            rule = PathBeneathAttr(access & handled, fd, 0)
            _syscall(numbers[1], ruleset_fd, _LL_RULE_PATH_BENEATH, ctypes.byref(rule), 0)
        finally:
            os.close(fd)

    for path, access in spec["rules"]:
        _add(path, access)
    _syscall(numbers[2], ruleset_fd, 0)
except OSError as exc:
    _fail("%s (errno %s)" % (exc.strerror or exc, exc.errno))

os.execvp(spec["argv"][0], spec["argv"])
"""

# The loader is a plain string (it cannot reference the module's constants
# because it runs with -I), so its two numeric holes are substituted here.
# (The create-ruleset flags argument is now the literal 0 — see the loader.)
_LOADER_SRC = _LOADER_SRC.replace("_LL_NO_NEW_PRIVS", str(_PR_SET_NO_NEW_PRIVS)).replace(
    "_LL_RULE_PATH_BENEATH", str(_LANDLOCK_RULE_PATH_BENEATH)
)

#: The ABI probe is process-stable (the kernel does not change under us);
#: cache it so a per-call backend build does not re-pay the syscall.
_ABI_CACHE: int | None | False = None


def _probe_abi() -> int:
    """The landlock ABI version, 0 when unsupported (never raises)."""
    global _ABI_CACHE
    if _ABI_CACHE is not False and _ABI_CACHE is not None:
        return int(_ABI_CACHE)  # type: ignore[arg-type]
    import ctypes

    if platform.machine() not in _SUPPORTED_MACHINES or sys.platform != "linux":
        _ABI_CACHE = 0
        return 0
    if getattr(sys, "_MEIPASS", None) is not None:
        # A frozen build: sys.executable is the app entrypoint, not a plain
        # interpreter, so the launcher form cannot work here.
        _ABI_CACHE = 0
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)

        class RulesetAttr(ctypes.Structure):
            _fields_ = [("handled_access_fs", ctypes.c_uint64)]

        libc.syscall.restype = ctypes.c_long
        ctypes.set_errno(0)
        result = libc.syscall(
            ctypes.c_long(_SYSCALL_CREATE_RULESET),
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint64(_LANDLOCK_CREATE_RULESET_VERSION),
        )
        abi = int(result) if result > 0 else 0
    except Exception:  # noqa: BLE001 - a probe failure is "unsupported"
        abi = 0
    _ABI_CACHE = abi
    return abi


def _handled_for_abi(abi: int) -> int:
    handled = 0x1FFF
    if abi >= 2:
        handled |= _REFER
    if abi >= 3:
        handled |= _TRUNCATE
    return handled


class LandlockBackend(SandboxBackend):
    """The landlock backend: kernel-level, allow-list rules, no external binary."""

    # A kernel exposing the landlock ABI is necessary but not sufficient: a
    # nested/containerised host can allow create+ruleset+restrict_self yet deny
    # execve under the resulting restriction (seccomp/no_new_privs interaction).
    # bwrap's probe already smoke-tests a real command; landlock must too, or
    # it reports "usable" and fail-closes every call with exit 126. Cached at
    # class level like the bwrap probe.
    _probe_cache: bool | None = None

    def probe(self) -> bool:
        """True only when a real command runs under the landlock launcher."""
        if LandlockBackend._probe_cache is not None:
            return LandlockBackend._probe_cache
        ok = False
        if _probe_abi() >= 1:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    argv, env = self.wrap(["/bin/true"], dict(os.environ), [Path(tmp)])
                    result = subprocess.run(
                        argv,
                        env=env,
                        capture_output=True,
                        timeout=TOOLS_TIMEOUTS["sandbox_bwrap_probe_timeout_seconds"],
                        check=False,
                    )
                    ok = result.returncode == 0
            except Exception:  # noqa: BLE001 - any probe failure means unavailable
                ok = False
        LandlockBackend._probe_cache = ok
        return ok

    def wrap(
        self,
        cmd: list[str],
        env: dict,
        writable_roots: Sequence[Path],
        *,
        allow_network: bool = False,
    ) -> tuple[list[str], dict]:
        """Wrap *cmd* in the landlock launcher (see the module docstring).

        ``allow_network`` is accepted for the shared contract and ignored:
        landlock (ABI ≤ 3) has no network hook, so the host network namespace
        is kept — the filesystem rules still apply.
        """
        roots = normalize_writable_roots(writable_roots)
        abi = _probe_abi()
        if abi < 1:
            raise RuntimeError("landlock unsupported on this kernel")
        handled = _handled_for_abi(abi)
        rules: list[list[object]] = []
        for path in _SYSTEM_READ_PATHS:
            rules.append([path, _READ_RIGHTS & handled])
        for root in roots:
            rules.append([str(root), _WRITE_RIGHTS & handled])
        for path in _SYSTEM_WRITE_PATHS:
            rules.append([path, _WRITE_FILE & handled])

        spec = {"abi": abi, "rules": rules, "argv": list(cmd)}
        payload = json.dumps(spec, separators=(",", ":"))
        if len(payload) > 100_000:
            # A pathological root list should never be able to break the spawn
            # through an env-var limit (Linux env entries cap around 128 KiB).
            raise ValueError("landlock rule spec too large")
        child_env = dict(env)
        child_env["SHERRY_LANDLOCK_SPEC"] = payload
        logger.debug("landlock: wrapping {} (abi {}, {} rules)", cmd[:1], abi, len(rules))
        return [sys.executable, *_LOADER_ARGUMENTS, _LOADER_SRC, "--", *cmd], child_env
