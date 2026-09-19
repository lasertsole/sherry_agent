"""Typed summary document for the auxiliary-LLM compression path.

The summarization middleware no longer asks the auxiliary model for a
free-form Markdown block: the model fills a :class:`SummaryDoc` through
``with_structured_output`` (``method="json_mode"`` — the model is fed the
field rules and replies with one JSON object), and the middleware renders the
existing Markdown template deterministically from the document.

Why a document instead of text:

* section presence is guaranteed by the schema, not by prompt obedience;
* item caps become array slices (``completed[-5:]`` …) instead of a fragile
  regex re-parse of free-form Markdown;
* the previous document travels on the chain as a structured payload
  (``additional_kwargs["summary_doc"]``) and is fed back to the model as
  JSON, so carry-forward is typed instead of "please copy the text";
* the same document always renders the same bytes, so the model-visible
  prefix stays cache-safe.

Field/section mapping (sections and order are identical to the old
``_SUMMARY_TEMPLATE``): ``latest_user_request`` → *Latest Unresolved User
Request* (verbatim, no truncation — it is the conversation anchor),
``goal`` → *Goal*, ``constraints`` → *Constraints & Preferences*,
``completed`` / ``in_progress`` / ``blocked`` → *Progress*,
``key_decisions`` → *Key Decisions*, ``next_steps`` → *Next Steps*,
``critical_context`` → *Critical Context*, ``relevant_files`` →
*Relevant Files*. ``active_plan_notes`` (plan-scoped lessons) and
``evicted_refs`` (``[evicted to: <path>]`` pointers) render as trailing
optional sections only when non-empty: the base sections are unchanged when
both arrays are empty.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config.features import SUMMARIZATION

__all__ = [
    "SummaryDoc",
    "cap_items",
    "cap_summary_doc",
    "render_summary_markdown",
]

COMPLETED_MAX_ITEMS = SUMMARIZATION["completed_max_items"]
KEY_DECISIONS_MAX_ITEMS = SUMMARIZATION["key_decisions_max_items"]
CRITICAL_CONTEXT_MAX_ITEMS = SUMMARIZATION["critical_context_max_items"]
ACTIVE_PLAN_NOTES_MAX_ITEMS = SUMMARIZATION["active_plan_notes_max_items"]
EVICTED_REFS_MAX_ITEMS = SUMMARIZATION["evicted_refs_max_items"]

_STRING_FIELDS = ("latest_user_request", "goal")
_LIST_FIELDS = (
    "constraints",
    "completed",
    "in_progress",
    "blocked",
    "key_decisions",
    "next_steps",
    "critical_context",
    "relevant_files",
    "active_plan_notes",
    "evicted_refs",
)

# Code-layer caps (the model is asked to keep lists small; the caps are the
# hard guarantee). Everything else in the document is uncapped.
_FIELD_CAPS: dict[str, int] = {
    "completed": COMPLETED_MAX_ITEMS,
    "key_decisions": KEY_DECISIONS_MAX_ITEMS,
    "critical_context": CRITICAL_CONTEXT_MAX_ITEMS,
    "active_plan_notes": ACTIVE_PLAN_NOTES_MAX_ITEMS,
    "evicted_refs": EVICTED_REFS_MAX_ITEMS,
}


class SummaryDoc(BaseModel):
    """Structured checkpoint of the compacted conversation prefix.

    Every field has a default, so a partially-filled JSON object (the model
    omitting an empty section) still validates. List items are coerced with
    ``str`` and a lone string is wrapped into a one-element list — that makes
    the ``json_repair`` fallback tolerant of common malformed shapes.
    """

    model_config = ConfigDict(extra="ignore")

    latest_user_request: str = Field(
        default="",
        description=(
            "The user's most recent unanswered request, quoted VERBATIM with no "
            "truncation (the conversation anchor). Empty string when none."
        ),
    )
    goal: str = Field(default="", description="One or two sentences describing the goal.")
    constraints: list[str] = Field(
        default_factory=list,
        description="Constraints, preferences and decisions that limit the work.",
    )
    completed: list[str] = Field(
        default_factory=list,
        description="Finished work items, oldest first (most recent items are kept).",
    )
    in_progress: list[str] = Field(default_factory=list, description="Work currently in progress.")
    blocked: list[str] = Field(default_factory=list, description="Blockers preventing progress.")
    key_decisions: list[str] = Field(
        default_factory=list, description='Decisions as "<decision>: <reason>", oldest first.'
    )
    next_steps: list[str] = Field(
        default_factory=list, description="Immediate next actions, in order."
    )
    critical_context: list[str] = Field(
        default_factory=list,
        description="Exact values, error strings, configs that must not be lost.",
    )
    relevant_files: list[str] = Field(
        default_factory=list, description='Files as "<path>: why it matters".'
    )
    active_plan_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Durable notes learned while executing the active plan, one line each "
            "(symptom -> avoidance). Copy existing notes forward verbatim; empty "
            "when no plan is active."
        ),
    )
    evicted_refs: list[str] = Field(
        default_factory=list,
        description=(
            'The "[evicted to: <path>]" eviction pointers observed in range. '
            "The pipeline maintains this list; carry existing entries forward."
        ),
    )

    @field_validator(*_STRING_FIELDS, mode="before")
    @classmethod
    def _coerce_str(cls, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    @field_validator(*_LIST_FIELDS, mode="before")
    @classmethod
    def _coerce_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return ["" if item is None else str(item) for item in value]
        return [str(value)]


def cap_items(items: list[str], max_items: int) -> tuple[list[str], int]:
    """Return ``(kept_tail, omitted_count)`` for one capped list.

    Arrays are chronological: the most recent items are the tail, so the cap
    keeps ``items[-max_items:]`` and reports how many older items were dropped
    (0 when nothing was dropped).
    """
    if max_items <= 0 or len(items) <= max_items:
        return list(items), 0
    return list(items[-max_items:]), len(items) - max_items


def cap_summary_doc(doc: SummaryDoc) -> SummaryDoc:
    """Apply every code-layer cap; idempotent.

    The capped document is what gets stored on the chain
    (``additional_kwargs["summary_doc"]``), so the carried payload cannot
    grow without bound.
    """
    updates: dict[str, list[str]] = {}
    for field_name, max_items in _FIELD_CAPS.items():
        kept, omitted = cap_items(getattr(doc, field_name), max_items)
        if omitted:
            updates[field_name] = kept
    if not updates:
        return doc
    return doc.model_copy(update=updates)


def _render_list_section(
    lines: list[str],
    header: str,
    items: list[str],
    *,
    cap: int | None = None,
    numbered: bool = False,
) -> None:
    lines.append(header)
    kept, omitted = cap_items(items, cap) if cap is not None else (list(items), 0)
    if kept:
        for index, item in enumerate(kept, start=1):
            lines.append(f"{index}. {item}" if numbered else f"- {item}")
    else:
        lines.append("- (none)")
    if omitted:
        lines.append(f"({omitted} earlier items omitted for brevity)")


def render_summary_markdown(doc: SummaryDoc) -> str:
    """Render the document into the existing summary Markdown template.

    Pure and deterministic: same document → same bytes (prefix-cache safe).
    The base sections and their order match ``_SUMMARY_TEMPLATE``; the two
    carrier sections (*Active Plan Notes*, *Evicted References*) are appended
    only when their array is non-empty, so a document without plan notes and
    without eviction pointers renders the exact legacy section skeleton.
    """
    lines: list[str] = []

    lines.append("## Latest Unresolved User Request")
    lines.append(f"- {doc.latest_user_request}" if doc.latest_user_request.strip() else "- (none)")
    lines.append("")

    lines.append("## Goal")
    lines.append(f"- {doc.goal}" if doc.goal.strip() else "- (none)")
    lines.append("")

    _render_list_section(lines, "## Constraints & Preferences", doc.constraints)
    lines.append("")

    lines.append("## Progress")
    _render_list_section(
        lines,
        f"### Completed (most recent {COMPLETED_MAX_ITEMS})",
        doc.completed,
        cap=COMPLETED_MAX_ITEMS,
    )
    lines.append("")
    _render_list_section(lines, "### In Progress", doc.in_progress)
    lines.append("")
    _render_list_section(lines, "### Blocked", doc.blocked)
    lines.append("")

    _render_list_section(
        lines,
        f"## Key Decisions (most recent {KEY_DECISIONS_MAX_ITEMS})",
        doc.key_decisions,
        cap=KEY_DECISIONS_MAX_ITEMS,
    )
    lines.append("")

    _render_list_section(lines, "## Next Steps", doc.next_steps, numbered=True)
    lines.append("")

    _render_list_section(
        lines,
        f"## Critical Context (most recent {CRITICAL_CONTEXT_MAX_ITEMS})",
        doc.critical_context,
        cap=CRITICAL_CONTEXT_MAX_ITEMS,
    )
    lines.append("")

    _render_list_section(lines, "## Relevant Files", doc.relevant_files)

    if doc.active_plan_notes:
        lines.append("")
        _render_list_section(
            lines,
            "## Active Plan Notes",
            doc.active_plan_notes,
            cap=ACTIVE_PLAN_NOTES_MAX_ITEMS,
        )
    if doc.evicted_refs:
        lines.append("")
        _render_list_section(
            lines,
            "## Evicted References",
            doc.evicted_refs,
            cap=EVICTED_REFS_MAX_ITEMS,
        )

    return "\n".join(lines)
