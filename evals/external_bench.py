"""Shared harness for container-backed external benchmarks.

Two external suites (``deepswe`` and ``terminal_bench``) need the same three
things: the benchmark's own Docker environment, a copy of its working tree the
agent's FILE tools can edit, and a ``terminal`` tool whose commands execute
INSIDE that environment. This module owns that bridge.

Design rules (the measurement is only as honest as these):

- the agent is the REAL main agent (``server.service.messages.async_generate``);
  only the ``terminal`` tool is replaced, by one that runs ``docker exec``;
- the working tree is bind-mounted into the container at its own host path AND
  at the task's container working directory, so a file edited by ``write_file``
  is the same bytes the container's tests see;
- each ``terminal`` call restores the previous call's cwd and exported
  environment, approximating the persistent tmux shell the official harnesses
  give their agents (a fresh ``docker exec`` alone would forget ``cd``/``export``);
- the benchmark's verifier is the judge; a harness failure (Docker missing, an
  image that cannot build, a verifier crash) is never reported as a model
  failure;
- every Docker-shaped prerequisite is checked BEFORE a model call. A suite that
  cannot run says so loudly and exits non-zero — a skipped suite is not a pass.

Docker is required at RUN time. Importing this module does not touch Docker, so
the unit tests and the suites' ``--dry-run`` path stay hermetic.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The benchmark clones live OUTSIDE the repository: a 500 MB task tree inside
#: ``evals/results`` would slow every repo-wide ripgrep/gate run (the same reason
#: the SWE-bench suite deletes its per-instance venvs).
CACHE_ROOT = Path(os.environ.get("SHERRY_EVAL_CACHE", str(Path.home() / ".cache" / "sherry_evals")))

#: Tools removed from the main agent for a container-backed arm. ``python_repl``
#: and the background-terminal tools would execute on the HOST, not in the task
#: container, and the web tools would hand the agent the internet the benchmark
#: withholds. ``terminal`` is not here: it is REPLACED, not removed.
DISABLED_TOOLS = frozenset(
    {
        "python_repl",
        "terminal_bg_list",
        "terminal_bg_output",
        "web_search",
        "web_fetch",
    }
)
_DISABLED_PREFIXES = ("browser_",)

#: Cap for one tool result (head + tail); the terminal tool's own eviction guard
#: is bypassed by the bridge, so the bridge owns the bound.
_OUTPUT_HEAD = 50_000
_OUTPUT_TAIL = 50_000

_STATE_DIR = "/tmp/.sherry-eval"
_STATE_ENV = f"{_STATE_DIR}/env"
_STATE_CWD = f"{_STATE_DIR}/cwd"


# ── Subprocess helpers ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandResult:
    """One subprocess result with stdout+stderr already merged."""

    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """Whether the process exited zero within its timeout."""
        return self.exit_code == 0 and not self.timed_out


def run(argv: list[str], *, timeout: float = 120.0, cwd: Path | None = None) -> CommandResult:
    """Run a host process, never raising for a nonzero exit or a timeout."""
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, f"{argv[0]}: {exc}")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return CommandResult(124, f"{stdout}{stderr}", True)
    return CommandResult(completed.returncode, f"{completed.stdout}{completed.stderr}")


def _docker(args: list[str], *, timeout: float = 120.0) -> CommandResult:
    return run(["docker", *args], timeout=timeout)


def docker_available() -> tuple[bool, str]:
    """Whether a Docker CLI and a reachable daemon are present."""
    if shutil.which("docker") is None:
        return False, "the docker CLI is not on PATH"
    result = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=20.0)
    if not result.ok:
        return False, result.output.strip()[-600:] or "docker info failed"
    return True, result.output.strip()


# ── Dataset acquisition ─────────────────────────────────────────────────────


def sync_repo(cache_dir: Path, url: str, pin: str, *, label: str) -> tuple[Path, str]:
    """Clone (once) and check out *pin*; return the checkout and its commit."""
    if not (cache_dir / ".git").is_dir():
        if cache_dir.exists() and any(cache_dir.iterdir()):
            raise RuntimeError(f"{label} cache exists but is not a git checkout: {cache_dir}")
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        print(f"[evals] cloning {label} into {cache_dir} …", flush=True)
        clone = run(
            ["git", "clone", "--depth", "1", url, str(cache_dir)],
            timeout=1800.0,
        )
        if not clone.ok:
            raise RuntimeError(f"could not clone {label}: {clone.output[-1500:]}")
    # A local checkout may sit at another commit; fetch the pinned one when the
    # object is not already present locally.
    head = run(["git", "-C", str(cache_dir), "rev-parse", "HEAD"], timeout=30.0)
    current = head.output.strip() if head.ok else ""
    if pin and current != pin:
        has = run(
            ["git", "-C", str(cache_dir), "cat-file", "-e", f"{pin}^{{commit}}"],
            timeout=30.0,
        )
        if not has.ok:
            fetch = run(
                ["git", "-C", str(cache_dir), "fetch", "--depth", "1", "origin", pin],
                timeout=900.0,
            )
            if not fetch.ok:
                raise RuntimeError(
                    f"could not fetch pinned {label} commit {pin}: {fetch.output[-1500:]}"
                )
        checkout = run(["git", "-C", str(cache_dir), "checkout", "--detach", pin], timeout=120.0)
        if not checkout.ok:
            raise RuntimeError(f"could not check out {pin}: {checkout.output[-1000:]}")
        current = pin
    return cache_dir, current


def local_or_cloned(
    env_var: str,
    cache_dir: Path,
    url: str,
    pin: str,
    *,
    label: str,
    subdir: str,
) -> tuple[Path, str]:
    """Resolve a task tree from ``<env_var>`` (a checkout or the tree itself)."""
    override = os.environ.get(env_var, "").strip()
    if override:
        root = Path(override).expanduser()
        tasks = root / subdir
        chosen = tasks if tasks.is_dir() else root
        if not chosen.is_dir():
            raise RuntimeError(f"{env_var} does not name a task tree: {root}")
        commit = "local"
        if (root / ".git").is_dir():
            head = run(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=30.0)
            commit = head.output.strip() if head.ok else "unknown"
        return chosen, commit
    checkout, commit = sync_repo(cache_dir, url, pin, label=label)
    return checkout / subdir, commit


# ── Docker containers ───────────────────────────────────────────────────────


def _created_id(result: CommandResult) -> str:
    if not result.ok:
        raise RuntimeError(f"docker run failed: {result.output[-1500:]}")
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("docker run returned no container id")
    return lines[-1]


def create_container(
    image: str,
    name: str,
    *,
    workdir: str | None = None,
    mounts: tuple[tuple[str, str], ...] = (),
    network: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> str:
    """Start a keep-alive container from *image* and return its id."""
    args = [
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "sherry-eval=1",
        "--entrypoint",
        "/bin/sh",
    ]
    for source, target in mounts:
        args += ["-v", f"{source}:{target}"]
    if workdir:
        args += ["-w", workdir]
    if network:
        args += ["--network", network]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    args += [image, "-c", "sleep 2147483647"]
    return _created_id(_docker(args, timeout=timeout))


def remove_container(container: str) -> None:
    """Best-effort force removal (a dead daemon must not mask a real result)."""
    if container:
        _docker(["rm", "-f", container], timeout=60.0)


def pull_image(image: str, *, timeout: float = 1800.0) -> CommandResult:
    """Pull an image; a present image returns fast (Docker checks the manifest)."""
    print(f"[evals] docker pull {image} …", flush=True)
    return _docker(["pull", image], timeout=timeout)


def build_image(tag: str, context: Path, *, timeout: float = 1800.0) -> CommandResult:
    """Build a task/verifier image from a context directory."""
    print(f"[evals] docker build -t {tag} {context} …", flush=True)
    return _docker(["build", "-q", "-t", tag, str(context)], timeout=timeout)


def image_exists(image: str) -> bool:
    """Whether a local Docker image with this reference exists."""
    return _docker(["image", "inspect", image], timeout=60.0).ok


def image_workdir(image: str) -> str:
    """The image's configured working directory (``/app`` when unset)."""
    result = _docker(
        ["image", "inspect", "--format", "{{.Config.WorkingDir}}", image],
        timeout=60.0,
    )
    value = result.output.strip()
    return value if result.ok and value else "/app"


def _extract_with_cp(container: str, workdir: str, host_dir: Path, timeout: float) -> CommandResult:
    return _docker(["cp", f"{container}:{workdir}/.", str(host_dir)], timeout=timeout)


def _extract_with_tar(container: str, workdir: str, host_dir: Path, timeout: float) -> bool:
    """Stream the container tree through the host tar (keeps host ownership)."""
    if shutil.which("tar") is None:
        return False
    try:
        producer = subprocess.Popen(
            ["docker", "exec", container, "tar", "-C", workdir, "-cf", "-", "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert producer.stdout is not None
        consumer = subprocess.Popen(
            ["tar", "-C", str(host_dir), "-xf", "-"],
            stdin=producer.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        producer.stdout.close()
        try:
            _, consumer_err = consumer.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            producer.kill()
            consumer.kill()
            return False
        producer_err = producer.stderr.read() if producer.stderr is not None else b""
        producer.wait(timeout=30.0)
        if producer.returncode == 0 and consumer.returncode == 0:
            return True
        detail = (producer_err + (consumer_err or b"")).decode("utf-8", "replace")
        print(
            f"[evals] tar extraction failed ({detail[-300:]}); falling back to docker cp",
            flush=True,
        )
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def _make_writable(root: Path) -> None:
    """Give the invoking user write access after a container extraction."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            mode = path.stat().st_mode
            # The container runs as root and may create root-owned files in the
            # bind mount. Opening the tree up lets the host-side file tools do
            # their atomic rename over them; this tree is a throwaway copy.
            wanted = mode | (0o777 if path.is_dir() else 0o666)
            if wanted != mode:
                path.chmod(wanted)
        except OSError:
            pass


def extract_workdir(
    image: str,
    workdir: str,
    host_dir: Path,
    *,
    name: str,
    timeout: float = 1800.0,
) -> None:
    """Copy the image's task tree to *host_dir* (fresh, writable by this user)."""
    if host_dir.exists():
        shutil.rmtree(host_dir)
    host_dir.mkdir(parents=True, exist_ok=True)
    container = create_container(image, name, workdir="/")
    try:
        check = _docker(
            ["exec", container, "/bin/sh", "-c", f"test -d {shlex.quote(workdir)}"],
            timeout=60.0,
        )
        if not check.ok:
            # A task may rely on a base image that omits the directory; the bind
            # mount creates it on the agent container anyway, so an empty tree is
            # the correct starting state.
            made = _docker(
                ["exec", container, "/bin/sh", "-c", f"mkdir -p {shlex.quote(workdir)}"],
                timeout=60.0,
            )
            if not made.ok:
                raise RuntimeError(
                    f"the task workdir {workdir} is unavailable in {image}: {made.output[-500:]}"
                )
        if os.name != "nt" and _extract_with_tar(container, workdir, host_dir, timeout):
            pass
        else:
            copied = _extract_with_cp(container, workdir, host_dir, timeout)
            if not copied.ok:
                raise RuntimeError(
                    f"could not copy {workdir} out of {image}: {copied.output[-1500:]}"
                )
        _make_writable(host_dir)
    finally:
        remove_container(container)


def start_agent_container(
    image: str,
    name: str,
    *,
    host_workdir: Path,
    workdir: str,
    network: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Start the task container with the host tree mounted at both paths."""
    mounts = [(str(host_workdir), str(host_workdir))]
    if str(workdir) != str(host_workdir):
        mounts.append((str(host_workdir), str(workdir)))
    print(
        f"[evals] starting {name} from {image} (tree {host_workdir} -> {workdir}, "
        f"network={network or 'default'}) …",
        flush=True,
    )
    return create_container(
        image,
        name,
        workdir=str(host_workdir),
        mounts=tuple(mounts),
        network=network,
        env=env,
    )


# ── The terminal bridge ─────────────────────────────────────────────────────


def _truncate(text: str) -> str:
    if len(text) <= _OUTPUT_HEAD + _OUTPUT_TAIL:
        return text
    omitted = len(text) - _OUTPUT_HEAD - _OUTPUT_TAIL
    return (
        text[:_OUTPUT_HEAD]
        + f"\n… [{omitted} chars omitted by the eval bridge] …\n"
        + text[-_OUTPUT_TAIL:]
    )


def _join_commands(commands: str | list[str]) -> str:
    if isinstance(commands, str):
        return commands
    return " && ".join(str(command) for command in commands)


class DockerBridge:
    """Executes shell commands inside one task container.

    Each call restores the cwd + exported environment of the previous call, so
    ``cd`` / ``export`` / ``source .venv/bin/activate`` behave like a persistent
    terminal. A background watchdog kills a hung command inside the container;
    the wrapper then still returns what was produced so far.
    """

    def __init__(self, container: str, *, host_workdir: Path) -> None:
        self.container = container
        self.host_workdir = str(host_workdir)
        self._state_env = f"{_STATE_ENV}-{uuid.uuid4().hex[:8]}"
        self._state_cwd = f"{_STATE_CWD}-{uuid.uuid4().hex[:8]}"

    def _script(self, command: str, timeout: int) -> str:
        cwd = shlex.quote(self.host_workdir)
        return (
            "set +e\n"
            f"mkdir -p {_STATE_DIR} 2>/dev/null\n"
            f'if [ -f "{self._state_env}" ]; then . "{self._state_env}" >/dev/null 2>&1; fi\n'
            f'__saved_cwd="$(cat "{self._state_cwd}" 2>/dev/null || true)"\n'
            f'if [ -n "$__saved_cwd" ] && cd "$__saved_cwd" 2>/dev/null; then :; else cd {cwd}; fi\n'
            f"( sleep {max(1, int(timeout))}; kill -TERM $$ ) >/dev/null 2>&1 &\n"
            "__watch=$!\n"
            f"{command}\n"
            "__rc=$?\n"
            'kill "$__watch" 2>/dev/null\n'
            f'pwd > "{self._state_cwd}" 2>/dev/null\n'
            f'export -p > "{self._state_env}" 2>/dev/null\n'
            "exit $__rc\n"
        )

    def _format(self, result: CommandResult, timeout: float) -> str:
        text = _truncate(result.output)
        if result.timed_out:
            text += f"\n[sherry-eval] command timed out after {timeout:.0f}s"
        elif result.exit_code != 0:
            text += f"\n[sherry-eval] exit code: {result.exit_code}"
        return text

    def exec(self, command: str, *, timeout: float = 300.0) -> str:
        """Run one command synchronously (the sync ``_run`` path)."""
        result = _docker(
            [
                "exec",
                "-w",
                self.host_workdir,
                self.container,
                "/bin/bash",
                "-lc",
                self._script(command, int(timeout)),
            ],
            timeout=timeout + 20.0,
        )
        return self._format(result, timeout)

    def exec_at(self, workdir: str, command: str, *, timeout: float = 300.0) -> str:
        """Run one fresh command at *workdir* (the benchmark's test phase)."""
        result = _docker(
            [
                "exec",
                "-w",
                workdir,
                self.container,
                "/bin/bash",
                "-lc",
                command,
            ],
            timeout=timeout + 20.0,
        )
        return self._format(result, timeout)

    async def aexec(self, command: str, *, timeout: float = 300.0) -> str:
        """Run one command without blocking the event loop."""
        argv = [
            "docker",
            "exec",
            "-w",
            self.host_workdir,
            self.container,
            "/bin/bash",
            "-lc",
            self._script(command, int(timeout)),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return "[sherry-eval] the docker CLI is not on PATH"
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout + 20.0)
            result = CommandResult(
                process.returncode or 0, (stdout or b"").decode("utf-8", "replace")
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            result = CommandResult(124, "", True)
        return self._format(result, timeout)

    def copy_tests(self, task_dir: Path) -> None:
        """Copy ``run-tests.sh`` + ``tests/`` into ``/tests`` like the harness."""
        mkdir = _docker(
            ["exec", self.container, "/bin/sh", "-c", "mkdir -p /tests"],
            timeout=60.0,
        )
        if not mkdir.ok:
            raise RuntimeError(f"could not create /tests: {mkdir.output[-500:]}")
        script = task_dir / "run-tests.sh"
        copied = _docker(["cp", str(script), f"{self.container}:/tests/"], timeout=300.0)
        if not copied.ok:
            raise RuntimeError(f"could not copy run-tests.sh: {copied.output[-1000:]}")
        tests = task_dir / "tests"
        if tests.is_dir():
            copied = _docker(["cp", f"{tests}/.", f"{self.container}:/tests/"], timeout=600.0)
            if not copied.ok:
                raise RuntimeError(f"could not copy tests/: {copied.output[-1500:]}")

    def copy_source(self, source: Path, target_dir: str) -> None:
        """Copy one host file into a container directory (the oracle script)."""
        mkdir = _docker(
            ["exec", self.container, "/bin/sh", "-c", f"mkdir -p {shlex.quote(target_dir)}"],
            timeout=60.0,
        )
        if not mkdir.ok:
            raise RuntimeError(f"could not create {target_dir}: {mkdir.output[-500:]}")
        copied = _docker(
            ["cp", str(source), f"{self.container}:{target_dir.rstrip('/')}/"],
            timeout=300.0,
        )
        if not copied.ok:
            raise RuntimeError(f"could not copy {source.name}: {copied.output[-1000:]}")

    def execute_tests(self, *, workdir: str, timeout: float) -> str:
        """Run the already-copied ``/tests/run-tests.sh`` in the task container."""
        return self.exec_at(
            workdir,
            "TEST_DIR=/tests bash /tests/run-tests.sh",
            timeout=timeout,
        )

    def run_tests(self, task_dir: Path, *, workdir: str, timeout: float) -> str:
        """Copy the task's tests in and run them at the task's working directory."""
        self.copy_tests(task_dir)
        return self.execute_tests(workdir=workdir, timeout=timeout)


# ── Agent-side integration ─────────────────────────────────────────────────


def _resolve_terminal_timeout(requested: int | None, *, default: int, maximum: int) -> int:
    if requested is None or requested <= 0:
        return default
    return min(int(requested), maximum)


def install_terminal_bridge(bridge: DockerBridge) -> None:
    """Replace the main agent's ``terminal`` tool with the container bridge."""
    from langchain_core.tools import BaseTool
    from pydantic import ConfigDict, Field

    import agent.core as agent_core
    from agent.tools.terminal import (
        TERMINAL_MAX_TIMEOUT,
        TERMINAL_TIMEOUT,
        SafeShellInput,
    )

    class DockerTerminalTool(BaseTool):
        """``terminal`` whose commands run in the task container."""

        name: str = "terminal"
        description: str = "Run shell commands on this Linux machine."
        args_schema: Any = SafeShellInput
        bridge: Any = Field(exclude=True)
        model_config = ConfigDict(arbitrary_types_allowed=True)

        @staticmethod
        def _command(commands: str | list[str]) -> str:
            return _join_commands(commands)

        @staticmethod
        def _timeout(requested: int | None) -> int:
            return _resolve_terminal_timeout(
                requested, default=TERMINAL_TIMEOUT, maximum=TERMINAL_MAX_TIMEOUT
            )

        def _run(
            self,
            commands: str | list[str],
            run_manager: Any = None,
            sandbox: bool = True,
            session_id: str = "",
            timeout: int | None = None,
            **kwargs: Any,
        ) -> str:
            del run_manager, sandbox, session_id, kwargs
            return self.bridge.exec(self._command(commands), timeout=self._timeout(timeout))

        async def _arun(
            self,
            commands: str | list[str],
            run_manager: Any = None,
            sandbox: bool = True,
            session_id: str = "",
            timeout: int | None = None,
            **kwargs: Any,
        ) -> str:
            del run_manager, sandbox, session_id, kwargs
            return await self.bridge.aexec(self._command(commands), timeout=self._timeout(timeout))

    tools = agent_core.get_agent_tools()
    replaced = False
    for index, tool in enumerate(tools):
        if getattr(tool, "name", "") == "terminal":
            tools[index] = DockerTerminalTool(
                bridge=bridge,
                metadata={"idempotent": False, "side_effect_scope": "system"},
            )
            replaced = True
            break
    if not replaced:
        tools.append(
            DockerTerminalTool(
                bridge=bridge,
                metadata={"idempotent": False, "side_effect_scope": "system"},
            )
        )


def localize_instruction(text: str, host_workdir: Path, workdir: str) -> str:
    """Rewrite container-absolute task paths to the host copy the agent edits.

    The file tools resolve paths on the HOST: a ``write_file('/app/x')`` from a
    task instruction would land outside the session's project directory. The
    same tree is mounted at both paths in the container, so showing the host
    path is byte-equivalent and keeps the file tools usable.
    """
    if not workdir or workdir == "/" or str(host_workdir) == workdir:
        return text
    pattern = re.compile(rf"(?<![\w/]){re.escape(workdir)}(?=/|\b)")
    return pattern.sub(str(host_workdir), text)


def trim_tools() -> None:
    """Drop host-executing / internet tools the benchmark arm must not have."""
    import agent.core as agent_core

    kept = []
    for tool in agent_core.get_agent_tools():
        name = getattr(tool, "name", "")
        if name in DISABLED_TOOLS or name.startswith(_DISABLED_PREFIXES):
            continue
        kept.append(tool)
    agent_core._tools[:] = kept


async def bind_external_session(
    session_id: str,
    repo: Path,
    *,
    append_note: str = "",
) -> dict[str, Any]:
    """Bind the session, grant approvals, and install the filtered tool set."""
    from evals.coding_capability import suite as harness

    await harness._bind_session(session_id, repo)

    import agent.core as agent_core
    from runtime.session.state_keys import StateKey
    from server.service.agent_config_service import invalidate_session_prompt
    from server.service.session_settings_service import _delete_value, _write_value

    payload = harness._coding_preset_payload()
    actual = {getattr(tool, "name", "") for tool in agent_core.get_agent_tools()}
    payload["tools"] = sorted(set(payload["tools"]) & actual)
    _write_value(session_id, StateKey.AGENT_CONFIG, payload)
    _delete_value(session_id, StateKey.AGENT_CONFIG_PENDING)
    invalidate_session_prompt(session_id)
    if append_note:
        print(f"[evals] session {session_id}: {append_note}", flush=True)
    return payload


# ── Judging / reporting ────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SUMMARY_RE = re.compile(r"=+\s*short test summary info\s*=+", re.IGNORECASE)
_RESULT_RE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|UNKNOWN)\b\s*(.*)$")
_PASSING = frozenset({"PASSED", "SKIPPED", "XFAIL"})


def parse_pytest_output(output: str) -> dict[str, str] | None:
    """Parse pytest's short summary the way Terminal-Bench's parser does.

    Returns ``None`` when no summary is present (the tests never ran); an empty
    mapping would otherwise make ``all(...)`` vacuously true.
    """
    text = _ANSI_RE.sub("", output)
    parts = _SUMMARY_RE.split(text, maxsplit=1)
    if len(parts) < 2:
        return None
    results: dict[str, str] = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        match = _RESULT_RE.match(line)
        if not match:
            continue
        status, rest = match.group(1), match.group(2).strip()
        if not rest:
            continue
        # A failure description starts with " - "; the node id never does.
        rest = rest.split(" - ", 1)[0].strip()
        name = rest.split("::", 1)[-1]
        if name:
            results[name] = status
    return results


def pytest_resolved(results: dict[str, str] | None) -> bool:
    """Terminal-Bench's rule: a non-empty result set with every test passing."""
    return bool(results) and all(status in _PASSING for status in results.values())


def read_reward(path: Path) -> dict[str, Any] | None:
    """Read DeepSWE's ``reward.json`` / ``reward.txt`` (``None`` on a crash)."""
    reward_json = path / "reward.json"
    if reward_json.is_file():
        try:
            payload = json.loads(reward_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload
    reward_txt = path / "reward.txt"
    if reward_txt.is_file():
        try:
            value = float(reward_txt.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return {"reward": value, "source": "reward.txt"}
    return None


def reward_resolved(payload: dict[str, Any] | None) -> bool:
    """The binary reward (1 = resolved). A missing reward is a harness crash."""
    if not payload:
        return False
    try:
        return float(payload.get("reward", 0)) >= 1.0
    except (TypeError, ValueError):
        return False


def publish(
    report: dict[str, Any],
    results_dir: Path,
    baseline_path: Path,
    *,
    update_baseline: bool,
) -> int:
    """Print the aggregate, maintain the baseline, and choose the exit code."""
    aggregate = report["aggregate"]
    baseline: dict[str, Any] | None = None
    threshold = 0.0
    if baseline_path.is_file():
        try:
            loaded = json.loads(baseline_path.read_text(encoding="utf-8"))
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            baseline = loaded
            threshold = float(loaded.get("threshold_resolve_rate") or 0.0)
            print(
                f"[evals] baseline: {aggregate['resolve_rate']} vs {threshold} "
                f"(run {loaded.get('captured_run')})",
                flush=True,
            )
    print(f"\n[evals] {report['suite']} aggregate: {json.dumps(aggregate)}", flush=True)
    print(f"[evals] report: {results_dir}", flush=True)

    if update_baseline or baseline is None:
        baseline_path.write_text(
            json.dumps(
                {
                    "captured_run": report["run_id"],
                    "threshold_resolve_rate": 0.0,
                    "model": report["model"],
                    "aggregate": aggregate,
                    "tasks": [task["id"] for task in report["tasks"]],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[evals] baseline updated: {baseline_path}", flush=True)

    harness_errors = [
        task["id"] for task in report["tasks"] if task.get("failure_kind") == "harness"
    ]
    if harness_errors:
        print(f"[evals] FAILED: harness error in task(s): {harness_errors}", flush=True)
        return 1
    if aggregate["resolve_rate"] < threshold:
        print(
            f"[evals] FAILED: resolve rate {aggregate['resolve_rate']} < threshold {threshold}",
            flush=True,
        )
        return 1
    return 0


def write_report(results_dir: Path, report: dict[str, Any]) -> None:
    """Persist the suite report where every eval suite keeps its run output."""
    (results_dir / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def maybe_reexec(marker_env: str, script_path: Path) -> None:
    """Re-exec in a clean interpreter so SHERRY_WORKSPACE_DIR wins at import."""
    if os.environ.get(marker_env) == "1":
        return
    env = {**os.environ, marker_env: "1"}
    completed = subprocess.run(
        [sys.executable, str(script_path.resolve()), *sys.argv[1:]],
        cwd=str(REPO_ROOT),
        env=env,
    )
    raise SystemExit(completed.returncode)


def run_id_now() -> str:
    """The timestamp that names this run's result directory."""
    return time.strftime("%Y%m%d_%H%M%S")


def container_name(suite: str, task_id: str) -> str:
    """A unique, Docker-safe container name for one task attempt."""
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", task_id).strip("-")[:50] or "task"
    return f"sherry-eval-{suite}-{slug}-{uuid.uuid4().hex[:6]}"


def image_tag(suite: str, task_id: str, suffix: str = "") -> str:
    """A Docker-safe image tag for one task's built environment."""
    slug = re.sub(r"[^a-z0-9_.-]+", "-", task_id.lower()).strip("-")[:50] or "task"
    tail = f"-{suffix}" if suffix else ""
    return f"sherry-eval-{suite}-{slug}{tail}".lower()
