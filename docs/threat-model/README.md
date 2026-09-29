# Threat Model

What Sherry defends against, where its trust boundaries are, and — just as
important — which protections are OS-enforced boundaries versus in-process
heuristics. It is the map; [the sandbox document](../sandbox/README.md) owns the
OS-isolation detail.

## Trust boundaries

| # | Boundary | Crossing | Enforced by |
|---|---|---|---|
| 1 | User → agent | WebSocket turn | Gateway auth (Origin allowlist + per-boot token) |
| 2 | LLM → tool | Tool call | HITL approval gate, `agent/middlewares/humanInTheLoop/detection.py` shell blocklist |
| 3 | Tool output → model context | Tool result | **Untrusted-output fence**, prompt-injection scanner, tool-result eviction |
| 4 | Child agent → parent | Announce pipeline | Completion gates, `SubagentCompletionDrain` |
| 5 | MCP server → agent process | Tool result | Untrusted-output fence (`mcp_` prefix rule; no MCP tool ships yet) |
| 6 | HTTP client → gateway | HTTP route | Gateway auth middleware, typed query-param casting |
| 7 | Filesystem → agent | File tools | `PathGuard`, `O_NOFOLLOW`, virtual-path resolution |
| 8 | Sandboxed child → host process | Subprocess spawn | `scrub_env`, `bwrap`/`seatbelt` isolation |
| 9 | Memory / skill files → system prompt | Prompt assembly | Skill scan gate on install; **write-time injection block pending** |
| 10 | TaskFlow step result → downstream step | DAG edge | Expectation→actual closure: schema gate, step judge, evidence ledger |

## Data classification

| Class | Examples | Where it lives |
|---|---|---|
| Sensitive | API keys, tokens | env vars; `scrub_env` (`agent/tools/pub_base/env_scrub.py`) strips them before any child process |
| Private | Conversation history | MesMemory SQLite (WAL) |
| Internal | Tool results | Message list + eviction files |
| Untrusted | Web page content, terminal output, MCP results | Tool messages — data to be read, never instructions to follow |

## Threat analysis

| Threat | Existing protection | Gap |
|---|---|---|
| Indirect prompt injection (tool output) | **Untrusted-output fence** (forged delimiters defanged), tool-result eviction, invisible-Unicode scan | — |
| Injected instructions in web/terminal text | Prompt-injection scanner (below) | Callers must apply it per surface |
| Memory / skill file injection | Skill scan gate on install | **Blocking scan on memory write** |
| Secret leakage into logs / tool output | **Redaction engine** on the log pipeline (always on) and on leak-prone tool output, `scrub_env` for child processes | Traceback locals under `diagnose=True` are not covered (see below) |
| URL credential leakage | **URL redaction**: extra schemes, nested percent-encoding (depth 8), query and presigned parameters | — |
| Path traversal | `PathGuard` + `O_NOFOLLOW` | — |
| Shell injection | `agent/middlewares/humanInTheLoop/detection.py` blocklist (12 hardline + 59 dangerous rules) | — |
| Sandbox escape | `bwrap` / `seatbelt` | — |
| Reasoning-block leakage into the stream | — | **Think scrubber** |
| Subprocess env hijack (`LD_PRELOAD`, `BASH_ENV`, …) | Name-based secret blocklist | **Hijack-variable block** |
| Upload endpoint content spoofing | Gateway auth (Origin + token) | **Byte-signature vs declared type check** |

## Untrusted-output fence

`agent/security/untrusted_wrapper.py`, applied by
`agent/middlewares/context_eviction/core.py` to every tool result on its way to
the model. Results from attacker-facing tools are wrapped in an
`<untrusted_tool_result source="…" id="…">` block whose advisory text states that
the content is data, not instructions.

| Property | How |
|---|---|
| Scope | `web_search`, `tavily_search` (the same tool ships under Tavily's own name once an API key is configured), `message_search`, and any `mcp_` tool |
| Delimiter forgery | A closing tag inside the payload is rewritten to `</untrusted-tool-result>` before wrapping, so it cannot end the block early |
| Ordering | Eviction runs first, so the block surrounds the preview the model is actually shown |
| Raw text | Only the model view is fenced; the message the inner persistence layer holds is never mutated |
| Switch | `UNTRUSTED_OUTPUT["enabled"]` in `config/features/agent_side/untrusted_output.py`, with the advisory wording beside it |

It is a **fence, not a boundary**: a model can still be talked into ignoring it.
Its value is that the boundary is now stated where the model reads, and that the
cheapest forgery (closing the block) is defanged.

## Secret redaction

`agent/security/redact.py` masks credentials in text and is applied at two
places, with different switches on purpose:

| Surface | Applied by | Switch |
|---|---|---|
| Every log record (console + the three rotating files) | `agent/security/redact_formatter.py` (a loguru patcher installed by `logs/logger.py`) | always on — a log file outlives the session and is read by people who never saw the secret |
| Tool output that leaks credentials for a living (`terminal`, `python_repl`, the untrusted set, `mcp_*`) | the same middleware that fences untrusted output | `REDACTION["tool_output_enabled"]` in `config/features/agent_side/redaction.py` |
| File tools (`read_file`, `patch_file`, `write_file`) | — | deliberately not redacted: the agent edits its own configuration, and masking a value would make a read-then-write lossy |

Families: vendor key prefixes (`sk-`, `ghp_`, `AKIA`, `xox*`, `AIza`, `hf_`, …),
secret-named assignments across every config shape (`.env`, INI, YAML, TOML, JSON
— including prefixed keys such as `"CURATOR_API_KEY"` and quoted values with
spaces), `Authorization` / `X-API-Key` headers, JWTs, PEM private-key blocks, URL
credentials and secret query parameters.

Properties worth relying on:

* **Idempotent** — the sentinel matches no family, so redacting twice changes
  nothing.
* **The switch is snapshotted at import** (`SHERRY_REDACT`): a model that talks
  its way into writing `export REDACT=false` cannot disarm a live session. The
  log path ignores the switch entirely.
* **Over-redaction is the chosen error.** A rule that requires the value to "look
  like" a credential would start missing all-lowercase passwords, so
  self-assignments of a secret-named variable in code are rewritten too (the rule
  itself carries the example); the corpus test pins
  the surfaces where rewriting would hurt (docs).
* **Known limit:** an exception's traceback is rendered from live frames, so a
  secret that only ever existed as a local variable inside the failing frame can
  still appear in the error sink's `diagnose` dump.

## Prompt-injection scanner

`agent/security/threat_patterns.py` scans attacker-controlled text and returns
finding IDs. Three tiers, each a superset of the one above:

| Scope | Adds | Use it for |
|---|---|---|
| `all` | Classic injection (ignore/disregard instructions, role hijack, system-prompt extraction), key exfiltration, hidden HTML comments | Every tool result |
| `context` | C2 / promptware shapes (node registration, heartbeat, tasking pull, known framework names), instruction-file rewrites | Tool results and context files |
| `strict` | SSH backdoors, shell-rc persistence, hardcoded provider secrets, invisible Unicode | Memory writes, skill installs |

```python
from agent.security.threat_patterns import scan_for_threats, first_threat_message

if findings := scan_for_threats(page_text, scope="context"):
    block_reason = first_threat_message(page_text, scope="context")
```

Properties its callers rely on:

* **It never raises and never mutates** the content it is given; an unknown scope
  falls back to `all`, because a typo must not take a tool down.
* **Scans are bounded** to the first 65,536 characters, and every quantifier in
  every pattern is bounded — a crafted input cannot turn the scan into a denial
  of service.
* **Messages never echo the matched text** — `first_threat_message` reports the
  finding ID only, so a finding can be logged or shown without re-injecting the
  payload.
* It is a **heuristic, not a boundary**. OS isolation (see
  [the sandbox document](../sandbox/README.md)) is the only hard boundary; the
  scanner raises the cost of an injection and gives the caller something to act
  on.

Measured against this repository's own artifacts: 3.0 MB of real log and
tool-output text across 112 files scans in 0.50 s (~6 MB/s) with **zero**
findings, while the same corpus with one injected payload reports it — the tiers
stay quiet on real content and still catch the shapes they exist for.
