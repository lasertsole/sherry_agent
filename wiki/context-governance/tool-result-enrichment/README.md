# 🔧 Tool-Result Enrichment Queue

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Part of [Context Governance](../README.md): a registry-based queue that appends advisory hints to a `ToolMessage`'s content after every tool call, modelled on the [system-reminder catalogue](../system-reminder/README.md) but triggered per tool call instead of per turn.

---

The enrichment middleware (`ToolResultEnrichmentMiddleware`, the result-appendix layer) is the **outermost** `wrap_tool_call` layer — registered before `ToolGuardrails`, so it sees the final result after every other middleware has processed it. After the handler returns, it runs every registered injector, collects non-`None` outputs, and injects them into the `ToolMessage.content`.

**Ordering contract:** because it is the outermost layer, enrichment runs on the way OUT — only after `ContextEvictionMiddleware` has already evicted or sliced the raw result. The advisory is appended to the model-visible (possibly evicted) `ToolMessage`, never to the raw text; the eviction layer never sees or rewrites the appended hint, and an already-offloaded result is enriched in its preview form.

## JSON-Aware Dual-Mode Injection

The middleware auto-detects the content type and injects accordingly:

| Content type | Injection mode | Example |
|---|---|---|
| Valid JSON dict | Add an `"advisory"` field inside the JSON object, re-serialize | `read_file`: `{"content": "...", "truncated": false}` → `{"content": "...", "truncated": false, "advisory": "[Advisory: ...]"}` |
| Plain text | Append `\n\n{suffix}` to the end | `terminal`: `"Exit code: 0\noutput..."` → `"Exit code: 0\noutput...\n\n[Advisory: ...]"` |

**Why JSON field injection is safe**: `read_file` returns `json.dumps({"content": ..., "truncated": bool, ...})`. Three sites in the eviction / truncation pipeline do `json.loads` on this content (`eviction.py`, `target_truncation.py`) — they only read the `content` and `truncated` keys. A new `"advisory"` field is ignored. No tool-level declaration or exclusion set is needed — the `try json.loads` auto-detection handles everything.

**Error results are skipped**: a `ToolMessage` with `status="error"` (ToolGuardrails BLOCK/HALT, HITL denial, etc.) is passed through untouched — a failed tool call does not receive hints.

## Registered Injectors

The catalogue registers injectors via `ToolResultInjector` dataclass (`agent/middlewares/tool_result_enrichment/core.py`). Each declares an `injector_id`, a `builder(tool_name, tool_args, result, session_id) → str | None`, a `priority`, an optional `tools_filter`, and `only_if_changed`.

| Injector | Priority | `only_if_changed` | Applies to | Injects |
|---|---|---|---|---|
| `hook_context` | 60 | `True` | All tools | `additionalContext` from `PostToolUse` hooks (`UserHooksMiddleware`), stashed by `_dispatch` and drained by the injector. |

**Priority ordering**: injectors are sorted by ascending priority — low priority is appended first (top of the suffix block), high priority last (bottom — closest to the model's attention).

## UserHooksMiddleware Wiring

`UserHooksMiddleware._dispatch` parses `additionalContext` from each hook's stdout JSON (`HookOutcome.additional_context`). In Step 1 (read-only events), this field was parsed but discarded. The enrichment queue wires it: `_dispatch` stashes the text via `stash_hook_context(session_id, text)`, and the `hook_context` injector drains the stash and appends it as `[Hook context]: ...` to the tool result. The hook cannot block, replace, or rewrite the result — only append text.

## Comparison with the System-Reminder Catalogue

| | Reminder catalogue | Tool-result enrichment |
|---|---|---|
| Trigger | Each turn's first model call (`before_model`) | Each tool call's return (`wrap_tool_call`) |
| Carrier | `HumanMessage` (separate message) | `ToolMessage.content` (field or append) |
| Priority sort | Budget allocation (drop lowest) | Append order (low first, high last) |
| Relationship | Orthogonal — coexist | Orthogonal — coexist |

The hint rides on the tool result the model already processes — it does not create a separate message that could hijack attention. For JSON tools (`read_file`), the hint is a field inside the JSON the model already parses; for plain-text tools, it is a suffix at the end of the content. In both cases the model's attention stays on the tool's real output, with the advisory seen peripherally.
