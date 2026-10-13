#!/usr/bin/env python
"""Real-machine smoke for the OS sandbox (run this on a host where bwrap works).

Why this exists: the sandbox is the only write barrier ``terminal`` /
``python_repl`` / ``execute_code`` have, and both backends' argv/profile logic is
unit-tested but was NEVER proven on a real machine with a working sandbox. The
P2 read whitelist (`SANDBOX_READ_ALLOWLIST=1`) is shipped OFF for exactly that
reason: enable it only after this script passes on your host.

What it checks, with and without the whitelist:

1. the probe resolves (a usable backend exists),
2. a write INSIDE the given root succeeds,
3. a write OUTSIDE the root fails (the write bypass is closed),
4. reading a file inside the root succeeds,
5. reading a file outside the root (*and* not a system path) fails when the
   whitelist is on — and lists what the whitelist still allows.

Usage::

    uv run --no-sync python scripts/sandbox_smoke.py [--root /path/to/project]
    SANDBOX_READ_ALLOWLIST=1 uv run --no-sync python scripts/sandbox_smoke.py

Exit code 0 = every check behaved as the contract says; 1 = a check failed (the
line names which). Run it as a normal user, not root — bwrap's user-namespace
path is what is being exercised.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools.pub_base.sandbox import get_backend, read_policy  # noqa: E402


def _run(argv: list[str], env: dict[str, str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603 - argv is built here, no shell
        argv,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", default=None, help="the writable root to probe (default: a temp dir)"
    )
    parser.add_argument("--outside", default="/", help="a path that must NOT be writable")
    args = parser.parse_args()

    policy = read_policy()
    backend = None
    try:
        backend = get_backend(policy)
    except RuntimeError as exc:
        print(f"FAIL backend probe: {exc}")
        print("     (install bwrap / allow user namespaces, or use macOS sandbox-exec)")
        return 1
    if backend is None:
        print(f"FAIL no usable backend (policy={policy.value}) — nothing to smoke on this host")
        return 1
    print(f"OK   backend={type(backend).__name__} policy={policy.value}")

    root = (
        Path(args.root).resolve() if args.root else Path(tempfile.mkdtemp(prefix="sandbox-smoke-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    inside = root / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside = Path(args.outside).resolve()
    whitelist = os.environ.get("SANDBOX_READ_ALLOWLIST", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    print(f"     root={root} outside={outside} read_allowlist={whitelist}")

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root)}
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if ok:
            print(f"OK   {label}")
        else:
            failures += 1
            print(f"FAIL {label}{(' — ' + detail) if detail else ''}")

    wrapped, wrapped_env = backend.wrap(["/bin/sh", "-c", "true"], env, [root])
    rc, out = _run(wrapped, wrapped_env, str(root))
    check("sandbox launches a trivial command", rc == 0, out.strip()[:200])

    wrapped, wrapped_env = backend.wrap(["/bin/sh", "-c", f"touch {root}/new.txt"], env, [root])
    rc, out = _run(wrapped, wrapped_env, str(root))
    check(
        "write INSIDE the root succeeds", rc == 0 and (root / "new.txt").exists(), out.strip()[:200]
    )

    target = outside / "sherry-sandbox-smoke.txt"
    wrapped, wrapped_env = backend.wrap(
        ["/bin/sh", "-c", f"touch {target} 2>/dev/null || true"], env, [root]
    )
    _run(wrapped, wrapped_env, str(root))
    check("write OUTSIDE the root is refused", not target.exists(), f"{target} was created")

    wrapped, wrapped_env = backend.wrap(["/bin/sh", "-c", f"cat {inside}"], env, [root])
    rc, out = _run(wrapped, wrapped_env, str(root))
    check("read INSIDE the root succeeds", rc == 0 and "inside" in out, out.strip()[:200])

    read_target = Path("/etc/hostname")
    if read_target.is_file() or Path("/etc").is_dir():
        wrapped, wrapped_env = backend.wrap(["/bin/sh", "-c", "cat /etc/hostname"], env, [root])
        rc, _out = _run(wrapped, wrapped_env, str(root))
        if whitelist:
            check("system config stays readable under the whitelist", rc == 0, f"rc={rc}")
        else:
            print(f"INFO /etc/hostname read rc={rc} (whitelist off: the whole root is readable)")

    if whitelist:
        probe = Path(tempfile.gettempdir()) / "sherry-smoke-hidden.txt"
        try:
            probe.write_text("hidden", encoding="utf-8")
            wrapped, wrapped_env = backend.wrap(["/bin/sh", "-c", f"cat {probe}"], env, [root])
            rc, _out = _run(wrapped, wrapped_env, str(root))
            check("non-whitelisted read fails under SANDBOX_READ_ALLOWLIST=1", rc != 0, f"rc={rc}")
            probe.unlink(missing_ok=True)
        except OSError:
            print("INFO could not create the hidden-read probe; skipped")

    if failures:
        print(
            f"\n{failures} check(s) failed — do NOT enable SANDBOX_READ_ALLOWLIST on this host yet."
        )
        return 1
    print("\nAll checks passed. This host's sandbox behaves as the contract says.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
