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
| Memory / skill file injection | **Memory-write scan** on all three write paths (a prose-specific table, because a note *about* the system is not an attack) + skill scan gate on install | — |
| Secret leakage into logs / tool output | **Redaction engine** on the log pipeline (always on, exception text included) and on leak-prone tool output, `scrub_env` for child processes | It removes what its patterns recognise, nothing more |
| URL credential leakage | **URL redaction**: extra schemes, nested percent-encoding (depth 8), query and presigned parameters | — |
| Path traversal | `PathGuard` + `O_NOFOLLOW` | — |
| Shell injection | `agent/middlewares/humanInTheLoop/detection.py` blocklist (12 hardline + 59 dangerous rules) | — |
| Sandbox escape | `bwrap` / `seatbelt` | — |
| Reasoning-block leakage into the stream | **Inline-CoT redirect**: the thinking is moved to the reasoning channel, not discarded | Models that emit no tags at all are unaffected by definition |
| Terminal control sequences in captured output | **Sequence stripper** on both `terminal` spawn paths | — |
| Channel identifiers (user / chat) in logs | **Stable pseudonym** at the channel log boundary | Routing tables and reply targets keep the raw value by design |
| Subprocess env hijack (`LD_PRELOAD`, `LD_LIBRARY_PATH`, `BASH_ENV`, …) | **Hijack-variable block**: startup hooks (`PYTHONPATH`, `BASH_ENV`, `ENV`, …) are always dropped; the loader variables (`LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_INSERT_LIBRARIES`) only under `SHERRY_STRICT_ENV_HIJACK=1`, because container runtimes set them for real | Loader variables are not blocked by default — see the note in the operations section |
| Cross-site request forgery | **Origin allowlist** plus the **CSRF guard**: `Sec-Fetch-Site: cross-site` is refused on mutating methods, and an `Origin`/`Referer` that is neither loopback nor allowlisted is refused too | A script client sends none of those headers; it is authenticated by the token, not by this guard |
| Server-side request forgery (SSRF) | **SSRF guard** on every fetched URL: non-global targets (private, loopback, metadata, RFC 2544, IPv6 ULA) are refused, the connection is pinned to the verified address, and each redirect hop is re-checked | Hosts behind a fake-ip proxy resolve public names into the refused range — the documented switch is the escape hatch |
| XSS in rendered content | Client DOMPurify allowlist + `vue/no-v-html` + **server security headers** (`script-src 'self'`, `object-src 'none'`, `nosniff`, framing) | The CSP is a second layer: the client sanitizer runs in the same context as the payload |
| Hand-rolled SSRF through `terminal` (a `curl` the model was talked into) | HITL approval patterns for cloud-metadata endpoints and for *written* loopback requests | A pattern, not a boundary: a command can be phrased around it |
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
* **The exception is covered too.** loguru renders a traceback from the live
  frames at emit time, so a patcher never sees that text; the patcher therefore
  renders it itself (through loguru's own formatter, so the `> File …` layout
  survives), redacts it, and clears the record's exception so no sink prints it
  again. That closes both paths a secret used to have out of a failing call: the
  exception's own message and the `diagnose` dump of frame values. It renders
  **without** those values on purpose — an annotated dump prints frame locals
  verbatim, and a chat id or a quoted message has no shape any rule recognises.

## Write and output boundaries

Four surfaces rewrite what leaves the process rather than what enters it. Each
was added because the raw form is useless or harmful downstream, and each is a
hygiene measure, not a boundary.

| Surface | Module | What it does |
|---|---|---|
| Memory writes (`memory` tool `add`/`replace` and the flush path) | `agent/tools/memory.py` | Screens the entry against a prose-specific pattern table and refuses the write; a blocked entry never reaches disk. It keeps its own table instead of the scanner tiers above because memory entries are prose *about* the system — a note naming `.bashrc` or a `KEY=` variable is a note, not an attack |
| Captured terminal output | `agent/security/terminal_output.py` | Drops CSI/OSC and the remaining escape sequences, C0 control bytes, and applies carriage-return overwrite semantics (`10%\r100%` → `100%`; a `\r\n` line ending keeps its text). Applied at BOTH `terminal` spawn sites (sync and async) |
| Answered text carrying inline reasoning | `agent/security/think_scrub.py` | Moves `<think>`/`<thinking>`/`<reasoning>` content from the answer channel to the reasoning channel the client renders as a thinking block. The per-turn scrubber is split-invariant: the provider decides where chunks break, so a tag cut anywhere must produce the same result as one chunk |
| Channel user/chat identifiers | `agent/security/pii.py` | Logs `«pii:<12 hex>»` instead of the platform identifier. Stable across processes, so "received" still correlates with "sent" in a later log file; the raw value stays where it functions (routing tables, reply targets, the platform SDK call) |

## Network boundary

Three gates stand between the process and the network, each at the boundary its
threat is actually reachable from:

| Boundary | Mechanism | Notes |
|---|---|---|
| Inbound mutations | `server/trigger/csrf.py` — mutating methods (`POST`/`PUT`/`PATCH`/`DELETE`) are refused when `Sec-Fetch-Site: cross-site`, or when an `Origin`/`Referer` is present and is neither loopback nor allowlisted | The browser-set header is what a page cannot forge; the `Origin` gate in `server/trigger/auth.py` stays the first layer, and a client that sends none of the three headers (curl, a test client) passes — the token authenticates it |
| Inbound responses | `server/trigger/security_headers.py` — `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy` on every response, refused ones included | `script-src 'self'` and `object-src 'none'` are the layers that survive a client-side sanitizer bypass; `GATEWAY["csp"]` overrides the policy, the literal `disabled` omits it |
| Outbound fetches | `pub/func/validator/public_url.py` (verdict) + `pub/func/validator/safe_fetch.py` (transport) | A target whose addresses are not *all* global is refused before a socket opens; the socket is then opened to the verified address itself (the hostname still travels in `Host`/SNI), so a second DNS answer cannot redirect the connection, and every redirect hop repeats the check |

The shell is a fourth path that a network guard cannot see: the HITL approval
list (`agent/middlewares/humanInTheLoop/detection.py`) carries patterns for cloud
metadata endpoints (the credential read whose body the model would summarize
back) and for written requests to loopback. A plain `curl http://127.0.0.1/…`
read stays ungated on purpose — prompting for every local probe trains the
operator to click through.

## Security policy

**The only hard boundary is the operating system.** Process isolation, file
permissions, the sandbox backends, and the gateway's auth boundary are the things
an attacker must actually defeat. Everything the agent does *in-process* is a
heuristic:

| Layer | What it is | Not a boundary because |
|---|---|---|
| Prompt-injection scanner, untrusted-output fence | Detection and labelling | A model can be argued past a fence; a pattern can be phrased around |
| Secret redaction, identifier pseudonyms, control-sequence stripping | Hygienic rewriting of text at an output boundary | They only remove what their rules recognise, and the raw value usually still exists upstream |
| Path guard, HITL allowlists, shell blocklists | Deny rules for known-bad shapes | Deny rules are incomplete by construction |
| Env scrubbing, lane limits, iteration budgets | Blast-radius reduction | They assume the child itself is not the attacker's code path |

Operating consequences we accept and state plainly:

* model output is untrusted input to whatever consumes it — never let the agent's
  words drive a privileged action without an OS-level check;
* anything the agent can read, it can eventually leak: give it only the
  credentials the task needs;
* ``terminal`` and ``python_repl`` run with the operator's OS identity, minus
  secret-named environment variables — so the OS permissions of that account are
  the real limit, not the sandbox *config*;
* a determined model that ignores the fence, or a malicious operator with the same
  account, is out of scope. We defend against *content* arriving from elsewhere,
  not against the person running the agent.

## Operations

**Boot checks.** The 128K context floor refuses to start (both LLMs); the gateway
mints a per-boot token, so a page open across a restart reconnects only after a
reload; secrets belong in ``.env`` (env vars only — the config files are not a
secret store).

**What to watch.** ``logs/output/error/`` carries the failures; the security-relevant
lines are:

| Signal in the log | Meaning |
|---|---|
| ``refusing WebSocket handshake`` | A client with a stale or missing token — expected after a restart |
| ``«redacted»`` in a message | A credential-shaped string reached a log record and was masked |
| ``«pii:…»`` in a message | A channel user/chat identifier was pseudonymised (the same string is the same chat) |
| ``csrf guard: refused`` | A mutating request arrived with a cross-site or foreign origin — a hostile page, or a client that lost its Origin |
| ``refused by the SSRF guard`` | A URL resolved to a non-global address and was not fetched |
| ``Potential security threat detected: <id>`` | The injection scanner fired on tool output |
| sandbox / denial lines | A tool call was refused by a deny rule |

**If you suspect a leak.** Rotate the credential first (环境配置 / ``.env``), then
search the logs for its prefix — the redaction sentinel tells you a value of that
shape *was* logged, and MesMemory holds the tool output it came from. A leak that
never had a recognised shape is exactly what the heuristic cannot rule out; assume
the worst for anything you cannot account for.

**Hardening knobs.** ``SHERRY_STRICT_ENV_HIJACK=1`` extends the child-env block to
the loader variables — correct for hosts that own their loader environment, wrong
on container runtimes that set them (this repository's development host does).
``UNTRUSTED_OUTPUT["enabled"]`` and ``REDACTION["tool_output_enabled"]`` switch the
model-path protections; ``SHERRY_REDACT`` is snapshotted at import, so a session
cannot turn redaction off by editing anything it can write.

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
