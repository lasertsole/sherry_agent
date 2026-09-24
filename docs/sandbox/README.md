# 🛡️ Tool Sandbox: Terminal, Python REPL & PTC

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent confines model-initiated code execution: environment scrubbing at every spawn, an OS-native sandbox when available, and a human approval gate for deliberate bypasses.

Three tools let the model execute code on your machine: `terminal` (shell commands), `python_repl` (Python in a child process), and PTC's `execute_code` (a longer Python script in a child process that can call real tools over an RPC bridge). A hallucinated or prompt-injected command could read API keys from the environment, write outside the project, or touch other processes. The sandbox layer confines all three.

Source of truth: `agent/tools/pub_base/env_scrub.py`, `agent/tools/pub_base/sandbox.py`, `agent/tools/pub_base/sandbox_bwrap.py`, `agent/tools/pub_base/sandbox_seatbelt.py`, `agent/tools/pub_base/path_utils.py`, `agent/tools/file_tools/`, `agent/tools/terminal.py`, `agent/tools/python_repl.py`, `agent/tools/ptc/runner.py`, `agent/middlewares/humanInTheLoop/`, `agent/middlewares/path_guard/`.

## Table of Contents

- [Overview & Threat Model](#-overview--threat-model)
- [Isolation Capabilities & Precedence](isolation/README.md)
  - [🧱 Isolation Capabilities](isolation/README.md#-isolation-capabilities)
  - [📊 Precedence Matrix](isolation/README.md#-precedence-matrix)
- [Implementation & Architecture](#%EF%B8%8F-implementation--architecture)
- [Configuration & Usage](#%EF%B8%8F-configuration--usage)
- [Testing](#-testing)
- [Honesty & Limitations](#%EF%B8%8F-honesty--limitations)

## 🎯 Overview & Threat Model

| Exposure | Without sandbox | Defense |
| :------- | :-------------- | :------ |
| **Secrets in env vars** | Child process inherits every variable, including `*_API_KEY` | L1 env scrubbing |
| **Filesystem writes** | Child writes anywhere the agent user can write | L2 OS sandbox (Linux / macOS) |
| **Filesystem reads** | Child reads `~/.ssh`, `.env`, credential stores | L2 read-shield (sensitive-path masking, Linux / macOS) + sensitive-file regex (terminal) |
| **File-tool path arguments** | A tool call asks `read_file` for a traversal or hard-denied path | §5 external-path gate + §6 structural gates + §7 `PathGuard` (external paths still go through HITL) |
| **Process / session scope** | Child shares namespaces and survives the parent | L2 `--unshare-all`, `--die-with-parent` |
| **Deliberate bypass** | Model asks for `sandbox=False` | Human approval gate (HITL) |
| **Programmatic tool calls (PTC)** | A generated `execute_code` child script calls real tools and could reach the filesystem directly | L1 env scrub + L2 OS sandbox wrap (same backend as `terminal` / `python_repl`) + restricted builtins + import allowlist |

Two layers plus one gate — plus a separate path-defense stack for the file tools:

- **L1. Environment scrubbing** (`scrub_env`): unconditional, at every spawn point, even when a human approved `sandbox=False`.
- **L2. OS-native sandbox**: bubblewrap on Linux, Seatbelt on macOS — write containment plus a sensitive-path read-shield (see [§2](isolation/README.md#2-os-native-sandbox-backends-l2)). Windows has no OS backend (see [Honesty & Limitations](#️-honesty--limitations)).
- **Human approval gate**: `sandbox=False` bypasses only in the main session, through a HITL interrupt.
- **PTC (`execute_code`)**: executor-only and exposes no `sandbox` flag, so it always requests sandboxing — the child argv is wrapped by the same L2 backend. `SANDBOX_POLICY=required` refuses the run and `auto` degrades with exactly one warning (see [Isolation §2](isolation/README.md#2-os-native-sandbox-backends-l2)).
- **File-tool path gate** ([Isolation §5–§7](isolation/README.md#5-external-file-path-gate-file-tools)): `resolve_project_path()`'s three structural gates and `O_NOFOLLOW` I/O, virtual-path rendering, search containment, the six-check external-path approval flow, and the `PathGuard` middleware screen.

## ⚙️ Implementation & Architecture

### Policy: `SandboxPolicy`

Three states parsed from the `SANDBOX_POLICY` environment variable:

| Value | Meaning |
| :---- | :------ |
| `required` | Backend unavailable ⇒ reject the command, never run unsandboxed |
| `auto` (default) | Backend unavailable ⇒ degrade to unsandboxed with one warning |
| `off` | Sandboxing disabled entirely |

`parse_policy` strips whitespace, matches case-insensitively, and raises `ValueError` on unknown values: a mistyped safety setting must fail loudly, never fall back silently. `read_policy()` calls `os.getenv` on **every** invocation (no import-time caching), so runtime changes take effect immediately.

### Backend contract and dispatch

`SandboxBackend` is the ABC every backend implements:

- `probe() -> bool`: must never raise; backends catch their own probe exceptions and return `False`.
- `wrap(cmd, env) -> (argv, env)`: returns the wrapped argv plus env, to be exec'd directly in list form (no shell).

`get_backend(policy)` dispatches:

1. `OFF` returns `None` immediately: no probe, no import, no subprocess.
2. Linux imports `BwrapBackend`, macOS imports `SeatbeltBackend` (lazy imports; `ImportError` means "unavailable", never a crash). Anything else, including Windows, has no backend.
3. If the backend exists but `probe()` fails: `REQUIRED` raises `RuntimeError("Required sandbox unavailable on {system}")`; `AUTO` / `OFF` return `None`.

### Tool integration

`SafeShellTool` (name `terminal`) and `TimedPythonREPLTool` (name `python_repl`) both expose a `sandbox: bool = True` parameter in their LLM-visible tool-call schema, so the model chooses per call. PTC's `ExecuteCodeTool` (name `execute_code`) has no `sandbox` parameter at all — it is executor-only and always requests the sandbox.

- **Sandboxed path**: `backend.wrap(["/bin/sh", "-c", cmd_str], env)` for terminal (semantically identical to POSIX `shell=True`), `backend.wrap([sys.executable, "-c", script], env)` for python_repl, and `backend.wrap([sys.executable, script_path], env)` for the PTC child. The wrapped argv is exec'd as a list, with no shell kwarg at all.
- **Fallback path (Windows / no backend)**: terminal joins commands with `" && "` and spawns with `shell=True`; python_repl spawns `[sys.executable, "-c", script]` as a list. Windows has **no** OS-sandbox backend.
- **Unconditional on every path**: `env=scrub_env()` and `cwd=str(ROOT_DIR)` (cwd clamp). Both tools enforce a 30-second timeout (`TERMINAL_TIMEOUT`, `PYTHON_REPL_TIMEOUT`) and kill the child on expiry; PTC enforces its own `ptc_timeout_seconds` and SIGKILLs the child's process group.
- **Error surfacing**: with `REQUIRED` and no backend, terminal wraps the `RuntimeError` into a `ToolException` (surfaced verbatim by `handle_tool_error=True`); python_repl surfaces the raw `RuntimeError`; PTC returns a `status: "sandbox_unavailable"` envelope before spawning anything.
- **Degrade warning**: when a sandboxed execution was wanted but no backend exists and the policy is not `off`, the tool layer logs exactly one loguru warning, then executes unsandboxed:

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`
  - `execute_code: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

## 🛠️ Configuration & Usage

### `SANDBOX_POLICY`

```bash
# .env or shell environment
SANDBOX_POLICY=auto      # required | auto | off (case-insensitive, default: auto)
```

Invalid values raise a `ValueError` at first use instead of silently using the default. The variable is re-read on every tool call, so you can flip it at runtime.

### `SHERRY_DENY_READ_PATHS`

```bash
# .env or shell environment (os.pathsep-separated; ~ is expanded)
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

Adds paths to the read-shield list of both OS backends (the defaults from [Isolation §2](isolation/README.md#2-os-native-sandbox-backends-l2) are always included). The variable is read on every `wrap()` call.

### What the model sees

Both tools accept a `sandbox` boolean per call, default `True`. The model is told that `false` executes with the scrubbed environment after human approval in the main session, and that subagents and background agents are refused.

### How a user approves or denies

When the model requests `sandbox=False` in the main session (non-YOLO), the graph suspends on a `HumanInTheLoop.after_model` interrupt. The frontend renders the action (tool name, full args, command or query) with two decisions:

- **approve**: resume with `{"decisions": [{"type": "approve"}]}`; the call runs immediately (env scrubbed, no OS sandbox).
- **reject**: resume with `{"decisions": [{"type": "reject", "message": "..."}]}`; the tool result becomes an error `ToolMessage` (`User denied: <msg>. <BLOCKED_MESSAGE>`) and nothing executes.

## 🧪 Testing

| Suite | Covers |
| :---- | :----- |
| `tests/agent/tools/test_sandbox_matrix.py` | 14 tests, one per matrix-cell behavior (cells 1-5 once per tool, cell 6 four times), including the real-graph HITL interrupt and the exactly-one-warning degrade assertion |
| `tests/agent/tools/pub_base/test_env_scrub.py` | scrub rules, precedence, keep/deny edges (29 tests) |
| `tests/agent/tools/pub_base/test_sandbox_policy.py` | policy parsing, strict `ValueError`, fresh-read semantics, dispatch |
| `tests/agent/tools/pub_base/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile construction (incl. read-shield mounts), probe caching (all subprocess mocked), plus an optional real-bwrap read-shield smoke test |
| `tests/agent/tools/pub_base/test_terminal_tool.py` / `test_python_repl_tool.py` | tool-level guards (dangerous / sensitive-file regexes), schema, spawn forms, restricted-builtins barrier |
| `tests/agent/tools/pub_base/test_path_utils.py` | the external-path flow, the three structural gates, symlink-loop handling, virtual-path rendering |
| `tests/agent/tools/file_tools/test_path_hardening.py` / `test_virtual_paths.py` / `test_search_containment.py` / `test_search_bounds.py` | symlink / TOCTOU refusal via `O_NOFOLLOW`, virtual paths, search-result containment, scan bounds |
| `tests/agent/middlewares/test_path_guard.py` | `PathGuard` screening: traversal components, hard-deny floor, external-path pass-through, structured error `ToolMessage` |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_characterization.py` | 19 tests locking pre-sandbox HITL / terminal legacy behavior |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_sandbox_bypass.py` | 17 tests for the bypass approval flow, YOLO pass-through, scope stamping |
| `tests/agent/tools/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` stamping |
| `tests/agent/tools/ptc/test_rpc_auth.py` / `test_sandbox_integration.py` / `test_runner_hardening.py` | PTC RPC one-time-token handshake; the PTC sandbox-policy mapping; runner token lifecycle and backend wrap |

Matrix tests patch `subprocess.Popen` globally, stub `get_backend` at the tool-module seam, and set `SANDBOX_POLICY` through the environment so the real `read_policy` runs in every cell.

## ⚠️ Honesty & Limitations

- **bwrap and Seatbelt construction logic is unit-tested but not verified on real Linux/macOS machines.** The backend source docstrings state this explicitly ("only the construction logic is verified, never run on a real Linux/macOS box"); all backend tests mock subprocess, and the read-shield has one optional real-bwrap smoke test that skips when the probe fails. Trust the wrap output, not yet a real containment guarantee.
- **Windows has no OS-sandbox backend.** Protection there is env scrubbing + cwd clamp + the dangerous-command regex + the sensitive-file regex + the HITL gate. Nothing prevents file writes outside the project root, and **read protection is unavailable**: without an OS backend there is no read-shield, so the application-level regexes are the only read gate.
- **The sensitive-file regex is a mitigation, not a barrier.** It matches literal command shapes; `dd`, `sed`, `python -c "open(…)"`, `$(< file)`, variables, and globs bypass it by design of the layer. The OS read-shield is the actual read barrier where a backend exists.
- **`python_repl` has no sensitive-file regex.** The terminal-only gates ([Isolation §3](isolation/README.md#3-dangerous-command-gate-terminal-only)) do not cover it; its wrapper script instead restricts builtins (the safe subset omits `open` / `__import__`) — a different, narrower control.
- **PTC inherits the same backend caveats.** `execute_code` now wraps its child with the L2 backend whenever one is usable, but under `SANDBOX_POLICY=auto` (the default) a host without a usable backend runs the child unsandboxed after one warning, and the bwrap construction's `--unshare-all` also unshares the network namespace — whether the loopback RPC bridge stays reachable under a real bwrap is unverified. PTC's one-time RPC token and its `/proc/<pid>/environ` residual are documented on the [PTC page](../ptc/README.md).
- **The degrade path executes unsandboxed by design.** `auto` + no backend = one logged warning, then a normal unsandboxed run. That is intentional availability-over-strictness; pick `SANDBOX_POLICY=required` if you need the opposite.
- **Env scrubbing is name-based.** A secret stored under a name without any blocked substring (and not on the deny list) passes through. There is no value scanning or dynamic secret detection, and that is deliberate.
- **No network sandboxing, seccomp, or AppArmor profiles are claimed or configured.** Isolation comes from the bwrap / Seatbelt constructions exactly as shown in [Isolation §2](isolation/README.md#2-os-native-sandbox-backends-l2), nothing more.
