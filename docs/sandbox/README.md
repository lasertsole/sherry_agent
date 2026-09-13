# 🛡️ Tool Sandbox: Terminal & Python REPL

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent confines model-initiated commands: environment scrubbing at every spawn, an OS-native sandbox when available, and a human approval gate for deliberate bypasses.

Two tools let the model execute code on your machine: `terminal` (shell commands) and `python_repl` (Python in a child process). A hallucinated or prompt-injected command could read API keys from the environment, write outside the project, or touch other processes. The sandbox layer confines all three.

Source of truth: `agent/tools/pub_base/env_scrub.py`, `agent/tools/pub_base/sandbox.py`, `agent/tools/pub_base/sandbox_bwrap.py`, `agent/tools/pub_base/sandbox_seatbelt.py`, `agent/tools/terminal.py`, `agent/tools/python_repl.py`, `agent/middlewares/humanInTheLoop/`.

## 🎯 Overview & Threat Model

| Exposure | Without sandbox | Defense |
| :------- | :-------------- | :------ |
| **Secrets in env vars** | Child process inherits every variable, including `*_API_KEY` | L1 env scrubbing |
| **Filesystem writes** | Child writes anywhere the agent user can write | L2 OS sandbox (Linux / macOS) |
| **Process / session scope** | Child shares namespaces and survives the parent | L2 `--unshare-all`, `--die-with-parent` |
| **Deliberate bypass** | Model asks for `sandbox=False` | Human approval gate (HITL) |

Two layers plus one gate:

- **L1. Environment scrubbing** (`scrub_env`): unconditional, at every spawn point, even when a human approved `sandbox=False`.
- **L2. OS-native sandbox**: bubblewrap on Linux, Seatbelt on macOS. Windows has no OS backend (see [Honesty & Limitations](#️-honesty--limitations)).
- **Human approval gate**: `sandbox=False` bypasses only in the main session, through a HITL interrupt.

## 🧱 Isolation Capabilities

### 1. Environment scrubbing (`scrub_env`), L1, unconditional

`scrub_env(base_env=None)` builds a safe environment dict for every child process. It is a pure function (only `os` / `re`, no IO, no logging) and never mutates its input; values are never inspected, only variable **names**. It runs at both spawn points of both tools, sync and async, and even on approved `sandbox=False` calls.

| Rule category | Match rule | Result | Examples |
| :------------ | :--------- | :----- | :------- |
| **Keep by exact name** | Case-insensitive exact name | Kept, wins over every deny rule | `PATH`, `HOME`, `USER`, `USERNAME`, `LANG`, `TERM`, `TMPDIR`, `TMP`, `TEMP`, `SHELL`, `LOGNAME`, `PYTHONPATH`, `PYTHONUTF8`, `VIRTUAL_ENV`, `COMPUTERNAME`, `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`, `OS`, `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`, `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH` |
| **Keep by prefix** | Name starts with `LC_`, `XDG_`, or `CONDA` | Kept, wins over every deny rule | `LC_ALL`, `XDG_CONFIG_HOME`, `CONDA_TOKEN` |
| **Force-deny (project secrets)** | Case-insensitive exact name | Always dropped | `MAIN_LLM_API_KEY`, `REASONER_LLM_API_KEY`, `AUXILIARY_LLM_API_KEY`, `TAVILY_API_KEY`, `LANGSMITH_API_KEY`, `ITTT_API_KEY`, `VTTT_API_KEY`, `TTI_API_KEY`, `RERANKER_API_KEY`, `EMBEDDING_API_KEY`, `STT_API_KEY` |
| **Substring block** | Name contains any of `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, `AUTH`, `DSN`, `WEBHOOK`, `BEARER`, `APIKEY` (case-insensitive) | Dropped | `MY_CUSTOM_TOKEN`, `AWS_SECRET_ACCESS_KEY` |
| **Pass-through** | No rule matches | Kept unchanged | `EDITOR`, `GIT_AUTHOR_NAME` |

- **Precedence**: keep (exact / prefix) > force-deny > substring block. `CONDA_TOKEN` survives the `TOKEN` substring rule via prefix keep; a name that merely *contains* `PATH` (e.g. `KEY_PATH_DELIM`) is not kept and is dropped by the `KEY` substring rule.
- This is **not an allowlist**: unmatched variables pass through untouched (an allowlist-only mode would break child processes by dropping `PATH`).
- Filtered names are never logged, so secret names cannot leak into logs.

### 2. OS-native sandbox backends (L2)

**Linux: bubblewrap (`bwrap`)**. The command runs inside a list-exec argv whose order is load-bearing:

```text
bwrap
  --ro-bind / /                              # whole root filesystem: READ-ONLY
  --bind <project root> <project root>       # the only writable locations:
  --bind <temp dir> <temp dir>               # project root + temp dir (deduped if equal)
  --tmpfs /tmp  --dev /dev  --proc /proc
  --unshare-all                              # all namespaces unshared
  --die-with-parent  --new-session
  --clearenv                                 # clear env BEFORE any --setenv
  --setenv <K> <V> ...                       # re-inject only the scrubbed vars
  -- /bin/sh -c "<command>"                  # the wrapped command
```

`--clearenv` before all `--setenv` turns the scrubbed dict into a real env allowlist. The root filesystem is read-only; writes land only in the project root and the temp dir.

**macOS: Seatbelt (`sandbox-exec`)**. The command runs as `sandbox-exec -p <profile> -- <cmd...>` with this profile:

```text
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "<project root>"))
(allow file-write* (subpath "<temp dir>"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/tty"))
```

Order is the spec: `(deny file-write*)` under `(allow default)` means "everything allowed except file writes", then explicit allows re-open the two writable paths plus the `/dev/null` and `/dev/tty` literals. Paths are embedded via `json.dumps`, so quotes or backslashes in a path cannot break out into injected sbpl forms.

**Probe (availability check)**. Both backends implement `probe() -> bool` with a class-level cache (probed once per process, failures cached too):

- `BwrapBackend.probe()`: a 3-second smoke run of `bwrap --ro-bind / / --proc /proc --dev /dev true`. Mere existence of the binary is not enough; on Ubuntu 24.04+ an AppArmor unprivileged-userns restriction can kill every bwrap at uid-map time, so a real smoke run is the only honest check.
- `SeatbeltBackend.probe()`: `shutil.which("sandbox-exec")` only; sbpl offers no exit-code based smoke probe.

### 3. Dangerous-command gate (terminal only)

`DANGEROUS_COMMAND_REGEX` is a blacklist of 6 alternative patterns, matched with `re.IGNORECASE` against the `" && "`-joined command string, before any spawn:

| # | Pattern idea | Catches |
| :- | :----------- | :------ |
| 1 | Recursive/force `rm` aimed at `/` or `~` | `rm -rf /`, `rm -fr ~` |
| 2 | Any recursive `rm` | `rm -r build/` |
| 3 | `mkfs` | filesystem reformatting |
| 4 | `shutdown` | system shutdown |
| 5 | `reboot` | system reboot |
| 6 | `|`, `&&`, or `;` followed by `rm` / `shutdown` / `reboot` / `mkfs` | chained variants such as `echo ok && rm -rf /` |

Matching the **joined** string matters: the older element-exact blacklist let `["echo ok", "rm -rf /"]` slip through because each element looked harmless alone. On a match the tool raises `ToolException("Blocked: unsafe command.")`, surfaced as an error tool result via `handle_tool_error=True`. The gate runs regardless of the `sandbox` flag. `python_repl` has no equivalent regex; its wrapper script restricts builtins instead.

### 4. Human-in-the-loop bypass approval

A `sandbox=False` call is a deliberate bypass request. In a **main-session** graph running the `HumanInTheLoop` middleware and not in YOLO mode, `after_model` parks the call on a LangGraph `interrupt()`:

- The interrupt payload shows the full tool call (tool name, args, command or query) with `allowed_decisions: ["approve", "reject"]`.
- **Approve** (`{"decisions": [{"type": "approve"}]}`): the call executes with its original args. The env is still scrubbed, cwd is still clamped to the project root, and the dangerous-command regex still applies. An approved bypass skips smart approval and the dangerous-command re-prompt, because the human approved this exact call; the hardline blocklist still ran first.
- **Reject** (or no decision): an error `ToolMessage` with content `User denied: <msg>. <BLOCKED_MESSAGE>` replaces the result. The command never executes and no second interrupt fires. `GraphInterrupt` is re-raised, never swallowed.
- **YOLO mode** (`is_yolo_mode`: `config.yolo_mode`, or `ApprovalMode.OFF`, or env `SHERRY_YOLO_MODE` in `1` / `true` / `yes`): the interrupt is skipped and the call executes directly (env scrub still applies).
- **Background / subagent scope**: heartbeat and cron tools are stamped `caller_scope="background"`; the subagent pipeline stamps `caller_scope="subagent"`. Those graphs have no HITL middleware, so the tool layer itself hard-rejects `sandbox=False` with a `ToolException`. No interrupt exists there, and none is needed.

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

`SafeShellTool` (name `terminal`) and `TimedPythonREPLTool` (name `python_repl`) both expose a `sandbox: bool = True` parameter in their LLM-visible tool-call schema, so the model chooses per call.

- **Sandboxed path**: `backend.wrap(["/bin/sh", "-c", cmd_str], env)` for terminal (semantically identical to POSIX `shell=True`) and `backend.wrap([sys.executable, "-c", script], env)` for python_repl. The wrapped argv is exec'd as a list, with no shell kwarg at all.
- **Fallback path (Windows / no backend)**: the original construction is kept byte-identical and only `env=` is added. Terminal joins commands with `" && "` and spawns with `shell=True`; python_repl spawns `[sys.executable, "-c", script]` as a list. Windows has **no** OS-sandbox backend.
- **Unconditional on every path**: `env=scrub_env()` and `cwd=str(ROOT_DIR)` (cwd clamp). Both tools enforce a 30-second timeout (`TERMINAL_TIMEOUT`, `PYTHON_REPL_TIMEOUT`) and kill the child on expiry.
- **Error surfacing**: with `REQUIRED` and no backend, terminal wraps the `RuntimeError` into a `ToolException` (surfaced verbatim by `handle_tool_error=True`); python_repl surfaces the raw `RuntimeError`.
- **Degrade warning**: when a sandboxed execution was wanted but no backend exists and the policy is not `off`, the tool layer logs exactly one loguru warning, then executes unsandboxed:

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

## 📊 Precedence Matrix

Authoritative table from `agent/tools/pub_base/sandbox.py`, tested cell-by-cell in `tests/integration/test_sandbox_matrix.py`:

| # | Policy | `sandbox` flag | Backend available? | Caller scope | Outcome |
| :- | :----- | :------------- | :----------------- | :----------- | :------ |
| 1 | `required` | `True` | yes | any | Executes inside the backend wrap (list-exec, scrubbed env) |
| 2 | `required` | `True` | no | any | `RuntimeError` / tool error; nothing is spawned |
| 3 | `required` | `False` | (not consulted) | any | Tool-layer `ToolException`, never a `GraphInterrupt`; no spawn |
| 4 | `auto` | `False` | (not consulted) | main, non-YOLO | HITL interrupt: approve → executes (still scrubbed), reject → error `ToolMessage` |
| 5 | `auto` | `True` | no | any | Degrade: direct unsandboxed execution, exactly one warning, env still scrubbed |
| 6 | `off` | `True` / `False` | never probed | main | No sandbox, no approval, no warning; executes directly |

Notes:

- `auto` + `True` + available backend behaves like cell 1: sandboxed via the backend wrap.
- The caller-scope guard is a tool-layer check that runs before policy handling: any non-main scope (`subagent`, `background`) requesting `sandbox=False` is hard-rejected with a `ToolException` under every policy, because no approval interrupt exists in those graphs. Cell 4's interrupt therefore only fires for main-scope calls.

## 🛠️ Configuration & Usage

### `SANDBOX_POLICY`

```bash
# .env or shell environment
SANDBOX_POLICY=auto      # required | auto | off (case-insensitive, default: auto)
```

Invalid values raise a `ValueError` at first use instead of silently using the default. The variable is re-read on every tool call, so you can flip it at runtime.

### What the model sees

Both tools accept a `sandbox` boolean per call, default `True`. The model is told that `false` executes with the scrubbed environment after human approval in the main session, and that subagents and background agents are refused.

### How a user approves or denies

When the model requests `sandbox=False` in the main session (non-YOLO), the graph suspends on a `HumanInTheLoop.after_model` interrupt. The frontend renders the action (tool name, full args, command or query) with two decisions:

- **approve**: resume with `{"decisions": [{"type": "approve"}]}`; the call runs immediately (env scrubbed, no OS sandbox).
- **reject**: resume with `{"decisions": [{"type": "reject", "message": "..."}]}`; the tool result becomes an error `ToolMessage` (`User denied: <msg>. <BLOCKED_MESSAGE>`) and nothing executes.

## 🧪 Testing

| Suite | Covers |
| :---- | :----- |
| `tests/integration/test_sandbox_matrix.py` | 14 tests, one per matrix-cell behavior (cells 1-5 once per tool, cell 6 four times), including the real-graph HITL interrupt and the exactly-one-warning degrade assertion |
| `tests/module/test_env_scrub.py` | scrub rules, precedence, keep/deny edges (29 tests) |
| `tests/module/test_sandbox_policy.py` | policy parsing, strict `ValueError`, fresh-read semantics, dispatch |
| `tests/module/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile construction, probe caching (all subprocess mocked) |
| `tests/module/test_terminal_tool.py` / `test_python_repl_tool.py` | tool-level guards, schema, spawn forms |
| `tests/module/test_hitl_characterization.py` | 19 tests locking pre-sandbox HITL / terminal legacy behavior |
| `tests/module/test_hitl_sandbox_bypass.py` | 17 tests for the bypass approval flow, YOLO pass-through, scope stamping |
| `tests/unit/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` stamping |

Matrix tests patch `subprocess.Popen` globally, stub `get_backend` at the tool-module seam, and set `SANDBOX_POLICY` through the environment so the real `read_policy` runs in every cell.

## ⚠️ Honesty & Limitations

- **bwrap and Seatbelt construction logic is unit-tested but not verified on real Linux/macOS machines.** The backend source docstrings state this explicitly ("only the construction logic is verified, never run on a real Linux/macOS box"); all backend tests mock subprocess. Trust the wrap output, not yet a real containment guarantee.
- **Windows has no OS-sandbox backend.** Protection there is env scrubbing + cwd clamp + the dangerous-command regex + the HITL gate. Nothing prevents file writes outside the project root.
- **The degrade path executes unsandboxed by design.** `auto` + no backend = one logged warning, then a normal unsandboxed run. That is intentional availability-over-strictness; pick `SANDBOX_POLICY=required` if you need the opposite.
- **Env scrubbing is name-based.** A secret stored under a name without any blocked substring (and not on the deny list) passes through. There is no value scanning or dynamic secret detection, and that is deliberate.
- **No network sandboxing, seccomp, or AppArmor profiles are claimed or configured.** Isolation comes from the bwrap / Seatbelt constructions exactly as shown above, nothing more.
