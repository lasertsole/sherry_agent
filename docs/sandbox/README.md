# 🛡️ Tool Sandbox: Terminal & Python REPL

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent confines model-initiated commands: environment scrubbing at every spawn, an OS-native sandbox when available, and a human approval gate for deliberate bypasses.

Two tools let the model execute code on your machine: `terminal` (shell commands) and `python_repl` (Python in a child process). A hallucinated or prompt-injected command could read API keys from the environment, write outside the project, or touch other processes. The sandbox layer confines all three.

Source of truth: `agent/tools/pub_base/env_scrub.py`, `agent/tools/pub_base/sandbox.py`, `agent/tools/pub_base/sandbox_bwrap.py`, `agent/tools/pub_base/sandbox_seatbelt.py`, `agent/tools/pub_base/path_utils.py`, `agent/tools/file_tools/`, `agent/tools/terminal.py`, `agent/tools/python_repl.py`, `agent/middlewares/humanInTheLoop/`, `agent/middlewares/path_guard/`.

## 🎯 Overview & Threat Model

| Exposure | Without sandbox | Defense |
| :------- | :-------------- | :------ |
| **Secrets in env vars** | Child process inherits every variable, including `*_API_KEY` | L1 env scrubbing |
| **Filesystem writes** | Child writes anywhere the agent user can write | L2 OS sandbox (Linux / macOS) |
| **Filesystem reads** | Child reads `~/.ssh`, `.env`, credential stores | L2 read-shield (sensitive-path masking, Linux / macOS) + sensitive-file regex (terminal) |
| **File-tool path arguments** | A tool call asks `read_file` for a traversal or hard-denied path | §5 external-path gate + §6 structural gates + §7 `PathGuard` (external paths still go through HITL) |
| **Process / session scope** | Child shares namespaces and survives the parent | L2 `--unshare-all`, `--die-with-parent` |
| **Deliberate bypass** | Model asks for `sandbox=False` | Human approval gate (HITL) |

Two layers plus one gate — plus a separate path-defense stack for the file tools:

- **L1. Environment scrubbing** (`scrub_env`): unconditional, at every spawn point, even when a human approved `sandbox=False`.
- **L2. OS-native sandbox**: bubblewrap on Linux, Seatbelt on macOS — write containment plus a sensitive-path read-shield (see [§2](#2-os-native-sandbox-backends-l2)). Windows has no OS backend (see [Honesty & Limitations](#️-honesty--limitations)).
- **Human approval gate**: `sandbox=False` bypasses only in the main session, through a HITL interrupt.
- **File-tool path gate** (§5–§7): `resolve_project_path()`'s three structural gates and `O_NOFOLLOW` I/O, virtual-path rendering, search containment, the six-check external-path approval flow, and the `PathGuard` middleware screen.

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
  --ro-bind /var/empty <sensitive dir>       # read-shield: mask sensitive directories
  --ro-bind /dev/null <sensitive file>       # read-shield: mask sensitive files
                                             # (--tmpfs <path> if /var/empty is absent)
  --tmpfs /tmp  --dev /dev  --proc /proc
  --unshare-all                              # all namespaces unshared
  --die-with-parent  --new-session
  --clearenv                                 # clear env BEFORE any --setenv
  --setenv <K> <V> ...                       # re-inject only the scrubbed vars
  -- /bin/sh -c "<command>"                  # the wrapped command
```

`--clearenv` before all `--setenv` turns the scrubbed dict into a real env allowlist. The root filesystem is read-only; writes land only in the project root and the temp dir.

**Read-shield (P0-1).** `--ro-bind / /` only makes reads possible-everywhere, not harmless: without masking, the model can `cat ~/.ssh/id_rsa`. Both backends therefore mask a default sensitive-path list — `DEFAULT_DENY_READ_PATHS` = `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/.docker` — resolved per call by `_sensitive_read_paths()` and extended by the `SHERRY_DENY_READ_PATHS` environment variable (`os.pathsep`-separated, `~` expanded; blanks skipped, order preserved, duplicates dropped). The bwrap shield mounts an empty directory over each existing directory (or `--ro-bind /dev/null` over a sensitive file); missing paths are skipped (there is nothing to read, and bwrap cannot create a mount point under the read-only root bind), and on hosts without `/var/empty` directories fall back to `--tmpfs <path>`. The shield sits **after** the writable binds so a writable project root can never re-expose a masked path.

**macOS: Seatbelt (`sandbox-exec`)**. The command runs as `sandbox-exec -p <profile> -- <cmd...>` with this profile:

```text
(version 1)
(allow default)
(deny file-write*)
(deny file-read* (subpath "<sensitive path>"))     # one per sensitive path, ~ expanded
(deny file-read* (regex #"(^|/)\.env$"))           # .env / .env.* at any depth
(deny file-read* (regex #"(^|/)\.env\."))
(allow file-write* (subpath "<project root>"))
(allow file-write* (subpath "<temp dir>"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/tty"))
```

Order is the spec: `(deny file-write*)` under `(allow default)` means "everything allowed except file writes", then explicit allows re-open the two writable paths plus the `/dev/null` and `/dev/tty` literals. The read-shield's `deny file-read*` rules sit directly after `(deny file-write*)`: one `subpath` rule per sensitive path (same default list + `SHERRY_DENY_READ_PATHS` as bwrap) and two regex rules covering `.env` / `.env.*` anywhere. Missing paths are still denied — a denial on a nonexistent path is harmless. Paths are embedded via `json.dumps`, so quotes or backslashes in a path cannot break out into injected sbpl forms.

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

**Sensitive-file gate (P0-2, `_SENSITIVE_FILE_PATTERNS`).** In both `_run` and `_arun`, `_check_sensitive_file_access(cmd_str)` runs **after** `_check_dangerous` and **before any spawn**: when any of the six compiled patterns matches the joined command string it raises `ToolException("Blocked: sensitive file access. …")` (`_SENSITIVE_FILE_MESSAGE`) — no child process is ever created — and the message routes the model to `read_file`, whose external paths go through human approval:

| Pattern | Catches |
| :------ | :------ |
| `(cat\|head\|tail\|less\|more) … /etc/(passwd\|shadow\|sudoers)` | system credential files |
| `(cat\|head\|tail) … .env` | `.env` / `.env.*` reads |
| `cp … .ssh/` | copying SSH material out |
| `curl … -d @… .env` | exfiltrating a dotenv via upload |
| `(cat\|head\|tail) … ~/.ssh/`, `(cat\|head\|tail) … ~/.aws/` | home credential stores |

**This is a mitigation, not a barrier.** `dd`, `sed`, `python -c "open(…)"`, `$(< file)`, shell variables, globs, and heredocs all bypass a literal regex — the real read barrier is the L2 read-shield above ([§2](#2-os-native-sandbox-backends-l2)), and an approved `sandbox=False` call is deliberately unsandboxed. The regex exists to stop the obvious, common attempts and to route the model to the approval flow.

### 4. Human-in-the-loop bypass approval

A `sandbox=False` call is a deliberate bypass request. In a **main-session** graph running the `HumanInTheLoop` middleware and not in YOLO mode, `after_model` parks the call on a LangGraph `interrupt()`:

- The interrupt payload shows the full tool call (tool name, args, command or query) with `allowed_decisions: ["approve", "reject"]`.
- **Approve** (`{"decisions": [{"type": "approve"}]}`): the call executes with its original args. The env is still scrubbed, cwd is still clamped to the project root, and the dangerous-command regex still applies. An approved bypass skips smart approval and the dangerous-command re-prompt, because the human approved this exact call; the hardline blocklist still ran first.
- **Reject** (or no decision): an error `ToolMessage` with content `User denied: <msg>. <BLOCKED_MESSAGE>` replaces the result. The command never executes and no second interrupt fires. `GraphInterrupt` is re-raised, never swallowed.
- **YOLO mode** (`is_yolo_mode`: `config.yolo_mode`, or `ApprovalMode.OFF`, or env `SHERRY_YOLO_MODE` in `1` / `true` / `yes`): the interrupt is skipped and the call executes directly (env scrub still applies).
- **Background / subagent scope**: heartbeat and cron tools are stamped `caller_scope="background"`; the subagent pipeline stamps `caller_scope="subagent"`. Those graphs have no HITL middleware, so the tool layer itself hard-rejects `sandbox=False` with a `ToolException`. No interrupt exists there, and none is needed.

### 5. External file path gate (file tools)

Independent of the L1/L2 sandbox above, file tools (`read_file`, `write_file`, `patch_file`, `search_files`, ...) resolve every path through `agent/tools/pub_base/path_utils.py::resolve_external_path()`, which applies six checks in order:

1. **Inside `ROOT_DIR`** — returned directly as a safe path.
2. **YOLO deny list** — the security floor: `~/.ssh/`, `~/.aws/`, `~/.gnupg/`, `~/.config/gcloud/`, `~/.env`, `~/.gitconfig`, `~/.npmrc`, `~/.pypirc`, extendable via `yolo_deny_paths` in `sherry.jsonc`. A hit is rejected here regardless of what follows: YOLO mode, an allowlist match, and subagent authorization inheritance all stop at this gate.
3. **YOLO mode** — global allow-all returns the path.
4. **Session allowlist** — exact-path entries and directory entries (trailing `/`, matching the directory and every descendant) are session-scoped and inherited by subagents.
5. **Subagent without prior authorization** — rejected: a subagent can never self-approve a new path.
6. **Main session** — HITL interrupt with `allowed_decisions: ["approve", "approve_dir", "yolo", "reject"]`:
   - `approve` — allow this file only (session-scoped, inherited by subagents);
   - `approve_dir` — allow the file's entire parent directory (session-scoped prefix match, inherited by subagents);
   - `yolo` — permanently allow all external paths;
   - `reject` — deny the access.

Inside `resolve_project_path()` (the ROOT_DIR side of the flow) the path passes three structural gates before any file I/O, and every open then refuses a symlink final component (`O_NOFOLLOW`); the module also renders model-visible paths as virtual paths that never contain `ROOT_DIR`. Those mechanisms, plus the search containment filter, are detailed in §6; the `PathGuard` middleware that screens path arguments before a tool ever runs is §7.

### 6. File-tool path gate: structural gates, no-follow I/O, virtual paths

File tools do not rely on a sandbox process: every project path is resolved in-process by `agent/tools/pub_base/path_utils.py`. `resolve_project_path()` applies three gates in order, before the tool touches the filesystem; when it rejects a path, the tool's `except PathOutOfBoundsError` branch re-routes it to the external-path HITL flow of §5.

1. **String-level rejection (`_reject_traversal_input`).** A `~` prefix or any `..` component is rejected with `PathOutOfBoundsError` before any filesystem access. The check is component-based (`Path(file_path).parts`), deliberately not a substring test: a substring test would false-positive on legitimate names such as `foo..bar` or `配置..md`.
2. **Containment.** Relative paths are joined onto `ROOT_DIR` (`~` expanded first), then `resolve()` runs; `resolved != ROOT_DIR and not resolved.is_relative_to(ROOT_DIR)` raises `PathOutOfBoundsError`. `ROOT_DIR` itself is allowed.
3. **Symlink-loop detection (`_raise_if_symlink_loop`).** `Path.resolve()` stops silently at a symlink loop and hands back the looping link; if the resolved path is a symlink, `stat()` maps that condition to `OSError(ELOOP)` (Linux/macOS, or Windows `winerror` 1921) and the error is re-raised instead of failing later in confusing ways.

**No-follow I/O (`_open_no_follow`).** Every read and write opens through `os.open(path, flags | O_NOFOLLOW, mode)`, so the final path component must not be a symlink: a link swapped in between resolution and open cannot redirect the I/O outside `ROOT_DIR` — the TOCTOU window is closed. The refusal raises `OSError(ELOOP)`, the same errno Linux/macOS produce natively; Windows has no `O_NOFOLLOW`, so the helper falls back to an explicit `path.is_symlink()` check raising the same error. `read_file` (read), `write_file` (write, plus read for the `.py` append/format flow) and `patch_file` (read + write) all go through it.

**Virtual-path rendering.** `to_virtual_path()` maps a real path under `ROOT_DIR` to a virtual one (`/src/main.py`). `display_path()` returns that virtual path normally, and falls back to `real_path.name or "/"` when the target is outside the root or unresolvable (`ValueError` / `OSError` / `RuntimeError`) — so `ROOT_DIR` never leaks. `safe_error_detail()` returns only `OSError.strerror` (e.g. `Permission denied`) or `UnicodeDecodeError.reason` (`invalid start byte`); every other exception's message is deliberately dropped, because generic exception text can embed the real root path (for example an error raised mid-`Path.rglob`), leaving only the exception type name. Net effect: model-visible results and error details never contain the real project root.

**Search containment (`_stays_within_root`).** `os.walk` does not descend directory symlinks, but file symlinks still surface in listings. Both search modes filter every hit through `_stays_within_root(candidate, root)` (`candidate.resolve().relative_to(root.resolve())`, skipping on `ValueError` / `OSError` / `RuntimeError`), so a symlink resolving outside the searched tree is never returned — a file symlink to `/etc/passwd` is skipped. The search root is always already resolved (project searches are additionally bounded by `ROOT_DIR`), which keeps allowlisted external-directory searches working.

**Design note vs. the deepagents reference.** The reference anchors every path to a virtual namespace (`virtual_mode`), making traversal structurally impossible by design; sherry keeps real filesystem paths — `prompt_builder`, the skill tools, and the terminal cwd all depend on them — and enforces containment *after* resolution (the gates above), closing the TOCTOU window with `O_NOFOLLOW`. Its `BackendProtocol`, `CompositeBackend`, `StateBackend`, and the full virtual path namespace were deliberately not adopted: that is an architecture rewrite, and sherry has no multi-backend use case.

### 7. `PathGuard` middleware

**Module:** `agent/middlewares/path_guard/core.py` · **Class:** `PathGuard(AgentMiddleware)` · **Hooks:** `wrap_tool_call` / `awrap_tool_call`

The file tools' gates only protect calls that reach them; `PathGuard` is the call-site screen registered in the main agent chain directly after `ToolCallNormalize` (`agent/core.py`). Because list order composes wrap hooks outermost-first, it runs **inside** `ToolGuardrails` (`IterationBudget` → `ToolGuardrails` → `PathGuard` → tool): a rejection is an ordinary error `ToolMessage` that ToolGuardrails evaluates like any other tool failure. It is not registered in the worker/subagent pipeline — child tools keep their own gates, and subagent external access is hard-denied anyway.

Screening is deliberately conservative:

- only string values under the argument names `file_path` / `path` / `directory` / `dir` are inspected; URL-shaped values (`scheme://`) are skipped, so non-path semantics are never misread as filesystem paths;
- `..` traversal components are rejected via the shared `has_traversal_component` predicate — URL-decoded and backslash-normalized first, so `%2e%2e` and `..\` cannot slip past; dot-only components (`...`) count as traversal too;
- a value `resolve_project_path()` accepts passes through untouched;
- a value resolving outside `ROOT_DIR` passes through **unless** it hits the hard-deny floor: `_SYSTEM_DENY_PATHS` (`/etc/passwd`, `/etc/shadow`, `/etc/sudoers`) or the YOLO deny list (`_is_yolo_denied`);
- every other external path is left to the tool's own `resolve_external_path()` HITL flow — the middleware never rewrites arguments and never raises an interrupt, so one call produces exactly one approval decision (the tool re-runs the same gate during execution; intercepting here would decide twice);
- missing or unresolvable targets and unknown exception classes fall through to the tool, which owns its error surface.

On a rejection `PathGuard` logs a warning and returns a structured error `ToolMessage` (`status="error"`, original `tool_call_id` and tool name) without executing the tool.

**Second line of defense.** The four file tools keep their own `resolve_project_path()` / `resolve_external_path()` calls, marked in code with `# redundant: path_guard middleware handles this — kept as the second line of defense` (`read_file`, `write_file`, `patch_file`, `search_files`). The middleware is the outer screen for a tool that might forget its own check; per-tool gates stay authoritative, and external paths still go through the human approval flow. Middleware-side details: [Middlewares README §PathGuard](../../agent/middlewares/README.md#pathguard).

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

Authoritative table from `agent/tools/pub_base/sandbox.py`, tested cell-by-cell in `tests/agent/tools/test_sandbox_matrix.py`:

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

### `SHERRY_DENY_READ_PATHS`

```bash
# .env or shell environment (os.pathsep-separated; ~ is expanded)
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

Adds paths to the read-shield list of both OS backends (the defaults above are always included). The variable is read on every `wrap()` call.

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
| `tests/agent/tools/file_tools/test_path_hardening.py` / `test_virtual_paths.py` / `test_search_containment.py` | symlink / TOCTOU refusal via `O_NOFOLLOW`, virtual paths, search-result containment |
| `tests/agent/middlewares/test_path_guard.py` | `PathGuard` screening: traversal components, hard-deny floor, external-path pass-through, structured error `ToolMessage` |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_characterization.py` | 19 tests locking pre-sandbox HITL / terminal legacy behavior |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_sandbox_bypass.py` | 17 tests for the bypass approval flow, YOLO pass-through, scope stamping |
| `tests/agent/tools/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` stamping |

Matrix tests patch `subprocess.Popen` globally, stub `get_backend` at the tool-module seam, and set `SANDBOX_POLICY` through the environment so the real `read_policy` runs in every cell.

## ⚠️ Honesty & Limitations

- **bwrap and Seatbelt construction logic is unit-tested but not verified on real Linux/macOS machines.** The backend source docstrings state this explicitly ("only the construction logic is verified, never run on a real Linux/macOS box"); all backend tests mock subprocess, and the read-shield has one optional real-bwrap smoke test that skips when the probe fails. Trust the wrap output, not yet a real containment guarantee.
- **Windows has no OS-sandbox backend.** Protection there is env scrubbing + cwd clamp + the dangerous-command regex + the sensitive-file regex + the HITL gate. Nothing prevents file writes outside the project root, and **read protection is unavailable**: without an OS backend there is no read-shield, so the application-level regexes are the only read gate.
- **The sensitive-file regex is a mitigation, not a barrier.** It matches literal command shapes; `dd`, `sed`, `python -c "open(…)"`, `$(< file)`, variables, and globs bypass it by design of the layer. The OS read-shield is the actual read barrier where a backend exists.
- **`python_repl` has no sensitive-file regex.** The terminal-only gates above do not cover it; its wrapper script instead restricts builtins (the safe subset omits `open` / `__import__`) — a different, narrower control.
- **The degrade path executes unsandboxed by design.** `auto` + no backend = one logged warning, then a normal unsandboxed run. That is intentional availability-over-strictness; pick `SANDBOX_POLICY=required` if you need the opposite.
- **Env scrubbing is name-based.** A secret stored under a name without any blocked substring (and not on the deny list) passes through. There is no value scanning or dynamic secret detection, and that is deliberate.
- **No network sandboxing, seccomp, or AppArmor profiles are claimed or configured.** Isolation comes from the bwrap / Seatbelt constructions exactly as shown above, nothing more.
