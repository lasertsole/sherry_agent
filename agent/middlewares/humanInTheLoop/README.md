# Human-In-The-Loop (HITL) Middleware

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

A comprehensive human-in-the-loop middleware for the hermes-agent pipeline. Provides layered approval gates for command execution (hardline/dangerous), file writes, MCP tool calls, destructive slash commands, and peer pairing — all managed through a single middleware hook.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Layer Reference](#layer-reference)
  - [1. Hardline & Dangerous Detection](#1-hardline--dangerous-detection)
  - [2. Write Approval Gate](#2-write-approval-gate)
  - [3. Interrupt Manager](#3-interrupt-manager)
  - [4. MCP Elicitation Consent](#4-mcp-elicitation-consent)
  - [5. Kanban Triage](#5-kanban-triage)
  - [6. Smart Approval](#6-smart-approval)
  - [7. Pairing Store](#7-pairing-store)
  - [8. Slash Confirm](#8-slash-confirm)
- [Middleware Hooks](#middleware-hooks)
- [Configuration](#configuration)
- [Approval Hook System](#approval-hook-system)
- [File Layout](#file-layout)

---

## Architecture Overview

The HITL middleware is composed of seven independent sub-gates, orchestrated by the `HumanInTheLoop` middleware class:

```
HumanInTheLoop
├── ApprovalPipeline      (approval.py — layered command approval)
│   ├── detect_hardline_command()
│   ├── detect_dangerous_command()
│   └── smart_approve()
├── WriteApprovalGate     (gates.py — file/memory write gating)
├── InterruptManager      (gates.py — per-session interrupt flags)
├── MCPElicitationConsent (gates.py — MCP server consent)
├── KanbanTriage          (gates.py — task failure triage)
├── PairingStore          (gates.py — platform user approval)
└── SlashConfirm          (gates.py — destructive slash confirmation)
```

Each gate is independently instantiable and testable. The `HumanInTheLoop` middleware wires them together and exposes them via the standard `AgentMiddleware` lifecycle hooks (`after_model`, `wrap_tool_call`, `awrap_tool_call`, `abefore_agent`).

---

## Layer Reference

### 1. Hardline & Dangerous Detection

**File:** `detection.py`

Two static pattern-matchers that classify commands without side effects:

| Function | Purpose |
|---|---|
| `detect_hardline_command(cmd)` | Checks against `HARDLINE_PATTERNS` — commands that must always be reviewed (`rm -rf`, `format`, `dd`, etc.) |
| `detect_dangerous_command(cmd)` | Checks against `DANGEROUS_PATTERNS` — commands with high destructive potential (`DROP TABLE`, `shutdown`, `rm`, force pushes) |

Both return the first matching pattern (string) or `None`.

### 2. Write Approval Gate

**File:** `gates.py` — class `WriteApprovalGate`

Manages pending write operations to file or memory targets. Each write is tracked by a unique ID and stored for approval/rejection:

| Method | Description |
|---|---|
| `request_write(target, content, session_id)` | Submit a write for approval. Returns `ApprovalResult` with a tracked `write_id`. |
| `approve_write(session_id, write_id)` | Approve a pending write. |
| `reject_write(session_id, write_id)` | Reject a pending write. |
| `get_pending_writes(session_id, target)` | List pending writes, optionally filtered by target type. |

### 3. Interrupt Manager

**File:** `gates.py` — class `InterruptManager`

Per-session boolean flags that gate tool execution mid-flight:

| Method | Description |
|---|---|
| `set_interrupt(session_id, active=True)` | Set or clear the interrupt flag. |
| `is_interrupted(session_id)` | Check whether a session is interrupted. |
| `clear_interrupt(session_id)` | Clear the interrupt flag (convenience alias). |

When an interrupt is set, the `wrap_tool_call` / `awrap_tool_call` hooks return a `ToolMessage` with status `"error"` and block execution.

### 4. MCP Elicitation Consent

**File:** `gates.py` — class `MCPElicitationConsent`

For MCP (Model Context Protocol) servers that may elicit side effects:

| Method | Description |
|---|---|
| `request_consent(server_name, session_id)` | Present an interrupt to the user requesting explicit consent for MCP server interaction. |

### 5. Kanban Triage

**File:** `gates.py` — class `KanbanTriage`

Tracks task failures for kanban-style triage escalation:

| Method | Description |
|---|---|
| `report_task_failure(task_id, session_id)` | Register a task failure. Returns `TriageStatus` (`NEW`, `ACKNOWLEDGED`, or `RESOLVED`). Raises `RecurrenceLimitError` if the failure count exceeds the configured `recurrence_limit`. |
| `resolve_triage(task_id, session_id)` | Mark a triaged task as resolved. |

### 6. Smart Approval

**File:** `approval.py` — class `ApprovalPipeline`

Configurable approval pipeline with multiple layers:

| Level | Mechanism |
|---|---|
| **Layer 1 — Hardline Detection** | Always-blocked commands (`rm -rf`, `format`, etc.) |
| **Layer 2 — Dangerous Detection** | Flagged commands (`DROP TABLE`, `shutdown`, etc.) |
| **Layer 3 — Terminal Mode** | Delegated to approval policy for terminal commands |
| **Layer 4 — Tool Approval** | Plugin-escalated tool approval (`request_tool_approval`) |
| **Layer 5 — Session Cache** | Approved tools cached per-session to avoid repeated prompts |
| **Layer 6 — Smart Approval** | `smart_approve()` — heuristic auto-approve/auto-deny based on command content and context |
| **Layer 7 — Human Interrupt** | Fallback to `interrupt()` for user decision |

The pipeline is exposed directly for external callers:

| Method | Description |
|---|---|
| `check_command(command, session_id)` | Run hardline + dangerous detection. Returns `ApprovalResult`. |
| `check_command_with_approval(command, session_id, prompt_fn)` | Full pipeline including smart approval + human interrupt. |
| `smart_approve(command)` | Heuristic-only approval (no detection or interrupt). |
| `request_tool_approval(name, args, session_id)` | Plugin-escalated tool approval check. |
| `approve_tool_for_session(name, args, session_id)` | Cache an approved tool for the remainder of the session. |

### 7. Pairing Store

**File:** `gates.py` — class `PairingStore`

Platform-level user allowlisting:

| Method | Description |
|---|---|
| `is_user_allowed(platform, user_id)` | Check whether a user is approved on a given platform. |
| `approve_user(platform, user_id)` | Add a user to the allowlist. |
| `revoke_user(platform, user_id)` | Remove a user from the allowlist. |

### 8. Slash Confirm

**File:** `gates.py` — class `SlashConfirm`

Confirmation gate for destructive slash commands (e.g., `/reset`, `/kill`):

| Method | Description |
|---|---|
| `confirm_destructive(action, session_id)` | Present an interrupt asking the user to confirm a destructive action. Returns `ApprovalResult`. |

---

## Middleware Hooks

The `HumanInTheLoop` class integrates into the agent lifecycle via four hooks:

| Hook | Purpose |
|---|---|
| `after_model` / `aafter_model` | Intercept the LLM output. For each tool call: run command approval, write-gate checks, `interrupt_on` config checks, and plugin-escalated approval. Replaces tool calls with artificial `ToolMessage` results when blocked. |
| `wrap_tool_call` | Check the interrupt flag before executing any tool. Returns an error `ToolMessage` if the session is interrupted. |
| `awrap_tool_call` | Async variant of `wrap_tool_call`. |
| `abefore_agent` / `before_agent` | Reset per-turn state (clear `turn_interrupted` flag). |

### Interrupt Flow

```
LLM output → after_model
  ├── Hardline/dangerous check (layers 1-2)
  ├── Write approval gate (memory writes only)
  ├── interrupt_on config check
  ├── Plugin tool approval (layer 4)
  └── Revised tool_calls + artificial ToolMessages

Each tool call → wrap/awrap_tool_call
  └── Interrupt flag check → block or pass
```

---

## Configuration

All configuration is passed through the `HITLConfig` dataclass (defined in `types.py`):

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | `ApprovalMode` | `STRICT` | `STRICT`, `SMART`, or `DISABLED` |
| `interrupted_tools` | `dict[str, bool \| dict]` | `{}` | Tool names gated by `interrupt_on` config. Each entry can be a boolean (default allowed decisions `["approve", "edit", "reject"]`) or a dict with `allowed_decisions` and optional `description` callable. |
| `interrupt_on` | deprecated | — | Replaced by `interrupted_tools`. |
| `write_approval_memory` | `bool` | `False` | Gate memory writes through `WriteApprovalGate`. |
| `description_prefix` | `str` | `"Agent wants to"` | Prefix for human-readable action descriptions. |
| `kanban_recurrence_limit` | `int` | `5` | Max failures before `RecurrenceLimitError` in KanbanTriage. |

### Example

```python
from agent.middlewares.humanInTheLoop import HumanInTheLoop, HITLConfig, ApprovalMode

middleware = HumanInTheLoop(HITLConfig(
    mode=ApprovalMode.SMART,
    interrupted_tools={
        "terminal": {"allowed_decisions": ["approve", "reject"]},
        "memory": True,
    },
    write_approval_memory=True,
    kanban_recurrence_limit=3,
))
```

---

## Approval Hook System

Register external callbacks that fire after every approval decision:

```python
def log_approval(session_id: str, result: ApprovalResult):
    print(f"[{session_id}] {result.decision}: {result.reason}")

middleware.register_approval_hook(log_approval)
```

Hooks receive the session ID and the full `ApprovalResult`. All hooks are wrapped in try/except — a failing hook never blocks the approval flow.

---

## File Layout

```
agent/middlewares/HumanInTheLoop/
├── __init__.py        # Public exports
├── types.py           # Enums, dataclasses, config, stubs
├── detection.py       # Hardline + dangerous pattern detection
├── approval.py        # Layered approval pipeline
├── gates.py           # Sub-gates (write, interrupt, MCP, kanban, pairing, slash)
├── core.py            # HumanInTheLoop middleware class
├── README.md          # This file (English)
└── README.zh.md       # Chinese version
```
