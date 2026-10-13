# 🧰 Programmatic Tool Calling (PTC)

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> The design-level companion to the [Subagent Design page](../subagent/README.md) — the two-axis role model that makes `execute_code` executor-only — to the [Subagent System README](../../agent/tools/subagent/README.md) — the runtime and API reference whose role table lists the tool — and to the [Tool Sandbox page](../sandbox/README.md) — the confinement stack that `terminal`, `python_repl`, and PTC's `execute_code` resolve. This page documents what PTC is, why a script executes in a separate process, how it reaches real tools, what the containment actually guarantees, and where it is honestly weaker than an OS sandbox.

Source of truth: `agent/tools/ptc/**`, `config/features/agent_side/ptc.py`, `config/features/agent_side/tools_timeouts.py`, `agent/tools/subagent/types/functional_role.py`, `agent/tools/subagent/spawn/core.py`, `agent/tools/subagent/spawn/system_prompt.py`, `agent/tools/pub_base/env_scrub.py`, `agent/tools/pub_base/sandbox.py`, and `tests/agent/tools/ptc/**`. Every statement below was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [Design Rationale](#-design-rationale)
- [Protocol and Artifacts](#-protocol-and-artifacts)
- [Security Model](#-security-model)
- [Configuration](#-configuration)
- [Limitations](#-limitations)
- [Failure Modes](#-failure-modes)
- [Test Map](#-test-map)
- [Related Documentation](#-related-documentation)

## 🎯 Overview

Programmatic Tool Calling (PTC) is the capability behind the `execute_code` tool: the model writes one Python script, and that script calls Sherry's real tools synchronously through generated wrappers. It targets the shapes a turn-by-turn tool loop handles badly — three or more tool calls with processing logic between them, loop- and branch-shaped tool use, and large tool outputs that should be filtered before they ever enter the model context.

| Question | Answer |
|----------|--------|
| Who can call it? | Only the `executor` functional role; the gate is `PTC_ROLES`, so the main agent and every other functional role never receive the tool |
| Where does the script run? | In a separate Python child process, spawned per invocation with its own process group, so a timeout kills the group |
| How does the script reach tools? | `from sherry_tools import ...` — a generated stub whose every wrapper is one newline-delimited JSON request over a loopback TCP socket to the parent RPC server |
| Whose session do tools run in? | The child's own `child_session_key`, bound when the tool is constructed and injected into every RPC dispatch — never a script argument |
| What comes back? | One JSON envelope with `status` (`ok` / `timeout` / `error` / `budget_exceeded` / `sandbox_unavailable`), `output`, `error`, `exit_code`, and `tool_calls_made` |

A script is ordinary Python plus the generated wrappers — no async, no context object, one synchronous call per tool:

```python
from sherry_tools import json_parse, read_file

raw = read_file("README.md")
print(json_parse(raw)["total_lines"])
```

```text
executor child agent
  └─ execute_code(code)
       └─ run_ptc: RPC listener + temp dir + sherry_tools.py stub + wrapper script
            └─ python child process  (restricted builtins, scrubbed env, own process group)
                 └─ from sherry_tools import read_file
                      └─ one-shot TCP to 127.0.0.1:<ephemeral port>
                           └─ parent event loop → real BaseTool.ainvoke(session_id=<child key>)
```

## 🧠 Design Rationale

**A separate process, not an in-process `exec`.** "Restricted builtins" and a "scrubbed environment" are only meaningful when the interpreter is disposable. The parent installs a reduced `__builtins__` namespace in the child and passes `env=scrub_env()` to a process it can SIGKILL. A wall-clock budget needs a killable process group; an accidental infinite loop, a crash, or a fork bomb stays inside the child. In-process `exec` would share the parent's globals and could not be terminated that way.

**A bridge, not tool objects.** Real tools are async `BaseTool`s that need the parent event loop, the provider configuration, and a session to resolve state against — the opposite of what belongs in a disposable interpreter. The generated stub sends arguments over the socket, and the parent dispatches each request onto its own loop with `asyncio.run_coroutine_threadsafe`, so a blocking child never blocks the parent's async work.

**The division of labor with `python_repl`.** Both surfaces execute model-authored Python with a mirrored restricted-builtins set and the same `scrub_env`, and both start the child in `ROOT_DIR`. They differ in shape: `python_repl` runs one snippet under a 30-second budget with no tool access, while `execute_code` runs a longer script (120-second budget) that can synchronously call up to 50 real tools. That extra reach is exactly why PTC is executor-only, and why it adds a process group, an RPC bridge, a call budget, and an import allowlist. The executor role keeps `python_repl`; PTC is additive, not a replacement. Both also resolve the same OS-native sandbox backend: the PTC child argv is wrapped exactly like a `sandbox=True` `python_repl` call (see the [Tool Sandbox page](../sandbox/README.md)), with `auto` degrading and `required` refusing as described under [Limitations](#-limitations).

**One named gate for the tool face.** `PTC_ROLES` — a single set in `agent/tools/subagent/types/functional_role.py` containing `executor` — is read by both the injection site (`spawn/core.py`, after the role allow/deny policy) and the prompt builder (`spawn/system_prompt.py`, one PTC guidance section). The tool face and its prompt section therefore cannot drift apart, and the executor role definition file stays free of PTC-specific wiring.

**A fail-closed tool set.** `build_ptc_tool` filters the child's available tools through the allowlist and removes `execute_code` itself. Listing a tool the executor does not carry is harmless; the intersection is what is enforced, and `sessions_spawn` / `sessions_yield` / `sessions_kill` / `sessions_steer` / `memory` / `skill_manage` / `question` are never eligible.

## 🔌 Protocol and Artifacts

Five modules, one responsibility each:

| Artifact | Responsibility |
|----------|----------------|
| `tool.py` | Defines `ExecuteCodeTool` (`execute_code`), binds the child session, intersects `available_tools` with `ptc_allowed_tools`, and renders the model-facing description from the live tool schemas |
| `runner.py` | `run_ptc` orchestrates one invocation: start the RPC server, create the temp dir, generate `sherry_tools.py`, write the wrapper script, generate a one-time RPC token, wrap the child argv with the OS-native sandbox backend, spawn the child, capture and truncate its output, then clean up |
| `rpc_server.py` | The loopback TCP listener: parses one JSON request per line, authenticates every frame against the per-run token, enforces the per-script call budget, and dispatches each call onto the parent event loop |
| `stub_generator.py` | Derives `ToolStub` specs from each tool's `tool_call_schema` and renders the `sherry_tools.py` module — one synchronous wrapper per tool plus local helpers; the RPC call reads the token from `SHERRY_PTC_RPC_TOKEN` rather than embedding it |
| `builtins.py` | Owns the child's reduced builtin namespace and the import allowlist, and renders both into the wrapper script |

The wire protocol is one JSON object per line, in both directions:

```json
{"tool": "read_file", "args": {"file_path": "README.md"}, "token": "<one-time token>"}
{"ok": true, "result": "..."}
{"ok": false, "error": "PTC call budget exhausted", "code": "PTCCallBudgetExceeded"}
```

**Signature fidelity.** Stub parameters come from `tool_call_schema`, not `args_schema`, so arguments the framework injects (such as `session_id`) never appear as callable parameters. A required field renders as a required parameter; an optional field renders with its default as a Python literal, and a default with no literal form falls back to `None` — the real tool re-validates every call anyway. The generated module also carries three local helpers that never touch the RPC bridge: `json_parse` (a lenient `json.loads`), `shell_quote` (`shlex.quote`), and `retry` (exponential backoff).

**Artifacts and lifecycle.** Each invocation gets a fresh `sherry_ptc_*` temp directory holding `sherry_tools.py` and the wrapper script; the child's only path addition is `PYTHONPATH=<tmpdir>`. The child environment also carries the per-run RPC token in `SHERRY_PTC_RPC_TOKEN`, which the generated stub reads and sends on every request; the token is generated with `secrets.token_urlsafe`, lives only in memory, and is never written to `sherry_tools.py` or any other file. When a backend is usable the child argv is additionally wrapped by `backend.wrap([sys.executable, script_path], env)` (the sandboxed path in [Tool Sandbox](../sandbox/README.md)); the process is still spawned with `start_new_session=True`, so a timeout SIGKILLs the whole group. The wrapper captures the user script's stdout and stderr into buffers, prints a single JSON envelope to the real stdout, and the runner classifies it into the `status` field. The `finally` block stops the RPC server, joins its thread, removes the temp directory, and re-kills the child if it somehow survived.

**Child session.** `spawn/core.py` passes the run's `child_session_key` as the tool's session, and `rpc_server.py` injects it into each `ainvoke` call via the runnable config. A script cannot supply or spoof a session: `session_id` is not a tool parameter at all.

## 🔒 Security Model

The controls below are capability reduction, not an OS boundary — the honest position is stated in [Limitations](#-limitations).

1. **Environment scrubbing.** The child environment starts from `scrub_env()`: variable names containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, `AUTH`, `DSN`, `WEBHOOK`, `BEARER`, or `APIKEY` (case-insensitive) are dropped, Sherry's own `*_API_KEY` variables are denied explicitly, and critical names such as `PATH` are kept by exact-name precedence. The runner then adds only `PYTHONPATH`, `PYTHONUNBUFFERED`, and the per-run RPC token (`SHERRY_PTC_RPC_TOKEN`).
2. **Restricted builtins.** The wrapper executes user code with a reduced `__builtins__`: `open`, `exec`, `eval`, `compile`, `globals`, `locals`, `input`, and `breakpoint` are absent, as are `exit`, `quit`, and `help`. `print`, collection types, and `getattr`-family introspection helpers remain.
3. **Guarded imports.** `__import__` is not removed wholesale — the import bytecode needs it — but it is replaced by a guard that resolves only the allowlist (`sherry_tools`, `json`, `re`, `math`, `time`, `csv`, `datetime`, `collections`, `itertools`, `functools`, `statistics`, `string`, `textwrap`, `decimal`, `random`, `fractions`, `heapq`, `bisect`). `import os` raises `ImportError`; `json` and `sherry_tools` pass.
4. **Per-script tool-call budget.** The RPC server refuses the 51st call: the budget check runs before dispatch, exceeding it raises `PTCCallBudgetExceeded`, and the envelope's status becomes `budget_exceeded`. An unknown tool name is rejected without consuming budget.
5. **Wall-clock timeout and process-group kill.** The child runs under `ptc_timeout_seconds` (120 s). On expiry the runner SIGKILLs the whole process group (`start_new_session=True`), so grandchildren die with it, then captures whatever output the pipes still hold and reports `status: "timeout"`.
6. **Output caps.** stdout is truncated at 50 KB and stderr at 10 KB, each with an explicit `...[truncated N bytes]` marker, before the envelope reaches the model.
7. **Loopback-only RPC with a one-time token.** The listener binds an ephemeral port on `127.0.0.1`; the constructor refuses `0.0.0.0`, `::`, and the empty host outright, and the bound address is re-checked before it is handed to the child. Every request frame must also carry the per-run token the child reads from `SHERRY_PTC_RPC_TOKEN`; the server compares it in constant time, and a missing or wrong token makes it drop the connection immediately, without a response, so a stray local process can neither invoke tools nor learn whether a guess was close.
8. **Child-session isolation.** Every dispatched tool call carries the child's `session_id` in its runnable config, so tools resolve state against the child session. The parent session is never exposed through PTC.
9. **Anti-recursion and privilege exclusion.** `execute_code` is not in its own allowlist, and the spawn, memory, skill-management, and question tools are excluded by configuration — a script cannot spawn agents, manage skills, or ask the user from inside the sandbox.
10. **Main-agent invisibility.** No PTC builder is registered in `_MAIN_TOOLS_BUILDERS`; the tool is constructed only inside child-agent assembly, and only when the role is in `PTC_ROLES`.

## ⚙️ Configuration

| Key | Default | Governs |
|-----|---------|---------|
| `ptc_timeout_seconds` | 120 | Wall-clock budget for one script, and the per tool-call dispatch window |
| `ptc_max_tool_calls` | 50 | Tool calls one script may dispatch before the budget error |
| `ptc_max_stdout_bytes` | 50000 | stdout retained from the child before truncation |
| `ptc_max_stderr_bytes` | 10000 | stderr retained from the child before truncation |
| `ptc_allowed_tools` | `read_file`, `write_file`, `patch_file`, `terminal`, `search_files`, `web_search` | Allowlist intersected with the child's available tools |

The object lives in `config/features/agent_side/ptc.py`; the shared per-tool registry in `config/features/agent_side/tools_timeouts.py` also declares `ptc_timeout_seconds` with the same 120-second default, while the runner's timeout comes from the `PTC` object. PTC adds no pip dependency: its implementation is the standard library plus the already-present LangChain and loguru packages.

## ⚠️ Limitations

- **It is not an OS-level sandbox on its own.** The restricted namespace and the import guard constrain what a literal script can name, but `getattr` and the other introspection builtins remain available, so a determined script can walk reachable objects and reach capabilities beyond its literal surface. That is the restricted-namespace tier — the same tier as the existing `python_repl` wrapper builtins. On top of it, the child argv is now wrapped by the same OS-native sandbox backend `terminal` and `python_repl` resolve (bubblewrap on Linux, Seatbelt on macOS) whenever a usable one exists, so it gains their write containment and read-shield. Two honest caveats remain: with `SANDBOX_POLICY=auto` (the default) and no usable backend the run degrades to unsandboxed after exactly one loguru warning, and the bwrap construction passes `--unshare-all`, which also unshares the network namespace — whether the loopback RPC bridge stays reachable under a real bwrap is not verified on a real Linux machine. See [Tool Sandbox](../sandbox/README.md) for the backends themselves.
- **Timeout cancellation is best effort.** When a tool call exceeds its dispatch window the server cancels the future it holds, but a coroutine already executing on the parent loop may still run to completion; the script receives a timeout error while the tool's side effects may continue.
- **Every invocation pays setup.** There is no warm pool: each `execute_code` call creates a temp directory, generates the stub, starts an RPC thread and a fresh interpreter, and tears everything down. A single trivial call costs a process launch.
- **The child's working directory is the repository root.** The runner defaults the child's `cwd` to `ROOT_DIR`, and the PTC tool does not forward a subagent's spawned working directory, so relative paths inside a script resolve from the repository root, not from wherever the executor was spawned.
- **Output caps are lossy by design.** Beyond 50 KB of stdout or 10 KB of stderr the tail is replaced by a byte-count marker; a script that prints a large table gets a truncated one.
- **Envelope-dependent reporting.** If the interpreter fails before the wrapper prints its envelope, there is no structured output to parse; the runner surfaces the raw stderr as the error and stdout may be empty.
- **The RPC token is not a cross-user boundary.** The listener now requires the one-time token, so a local process that cannot read it can no longer invoke tools or even complete a request. But the token travels through the child's environment, so any *same-user* process that can read `/proc/<pid>/environ` during the script's short lifetime can still recover it; the token defends against unrelated local processes, not against a same-user attacker. The listener also still binds loopback only and lives only for the duration of one script.

## 🧯 Failure Modes

| Failure | Observable | Degradation |
|---------|------------|-------------|
| Budget exhausted | `status: "budget_exceeded"`, error naming `PTCCallBudgetExceeded` | Further calls keep failing; the script may catch the error, and the parent turn still receives the partial output |
| Wall-clock timeout | `status: "timeout"`, a negative `exit_code` from the SIGKILL | The process group is dead; partial stdout and stderr are captured up to the caps; the parent turn continues normally |
| Sandbox required but unavailable | `status: "sandbox_unavailable"`, error naming `SANDBOX_POLICY=required` | The run is refused before any child is spawned; the parent turn gets an actionable hint to install bubblewrap / set `SANDBOX_POLICY=auto` |
| Output truncation | `output` or `error` ends with `...[truncated N bytes]` | The script completed; only the copy returned to the model is clipped |
| Tool error | RPC response `{"ok": false, "error": "TypeError: ..."}` | The stub raises `RuntimeError`; a script may catch it, and an uncaught one lands in the envelope's `error` |
| Import denied | `ImportError: import of 'os' is not allowed in PTC` | The script may catch it; uncaught, it lands in the envelope's `error` |
| RPC disconnect | Stub raises `RuntimeError: PTC RPC connection closed before a response arrived` | The server returns to `accept` and serves a reconnecting child; a script that does not retry fails with the error envelope |
| Unknown tool name | RPC response `{"ok": false, "error": "unknown tool: ..."}` | The call is rejected without consuming budget, so the script can fall back to another tool |
| Interpreter-level failure | No envelope; raw stderr becomes `error`, `status: "error"` | stdout may be empty; the model sees the interpreter's own message instead of a structured report |

## 🗺️ Test Map

| Area | Tests |
|------|-------|
| Tool surface: identity, allowlist intersection, bound session, anti-recursion | `tests/agent/tools/ptc/test_tool.py` |
| Stub generation: signature matching against real schemas, injected-argument exclusion, local helpers | `tests/agent/tools/ptc/test_stub_generator.py` |
| RPC protocol: loopback refusal, JSON round-trip, budget, tool errors, reconnect, stop | `tests/agent/tools/ptc/test_rpc_server.py` |
| RPC token handshake: correct / missing / wrong, immediate connection close on rejection | `tests/agent/tools/ptc/test_rpc_auth.py` |
| Sandbox policy mapping: backend wrap, `required` refusal, `auto` degrade with one warning, `off` | `tests/agent/tools/ptc/test_sandbox_integration.py` |
| Runner hardening: per-run token uniqueness, token absent from the generated stub file, backend wrap applied to the child argv, the single degrade warning | `tests/agent/tools/ptc/test_runner_hardening.py` |
| Child process: end-to-end tool call, restricted builtins, import gate, timeout kill, truncation, env scrubbing, temp-dir cleanup | `tests/agent/tools/ptc/test_runner.py` |
| Role grid: executor injection, the other four roles, prompt guidance, main-registry isolation | `tests/agent/tools/ptc/test_integration.py` |
| Real-LLM end to end: an executor child chooses `execute_code` and reads a file over RPC | `tests/agent/tools/subagent/test_ptc_executor_e2e.py` |

The five PTC suites run in the hermetic groups (`unit`, `integration`, `module`). The end-to-end file is the only `llm_e2e` test: it is deselected by default, runs in the dedicated real-LLM job, and proves the actual model path — the child must call `execute_code`, the captured envelope must report a dispatched RPC call, and the printed line count must match a count computed from the file at test time, which the sandbox cannot produce without `read_file` because `open` is absent there.

## 🔗 Related Documentation

| Page | What it covers |
|------|----------------|
| [Subagent Design](../subagent/README.md) | The two-axis role model (depth role × functional role) and why `execute_code` is an executor-only face |
| [Subagent System README](../../agent/tools/subagent/README.md) | Runtime and API reference: spawn pipeline, role table with the tool list, and registry behavior |
| [Tool Sandbox](../sandbox/README.md) | Environment scrubbing and the OS-native isolation that `terminal`, `python_repl`, and PTC's `execute_code` resolve — including the `auto` degradation and `required` refusal PTC inherits |
