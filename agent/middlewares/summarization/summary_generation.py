"""LLM summary generation: prompt templates, chaining, JSON repair, fallbacks.

The :class:`SummaryGenerationMixin` methods run on the middleware instance but own
only summarization-prompt concerns; the process-level seams they need
(``_taskflow_context`` / ``_plan_context`` / ``_resolve_active_plan``) are
provided by ``summarization.core.Summarization``.
"""

# allow: SIZE_OK — the bulk is the fixed prompt-template surface (data) plus
# the structured -> json_repair -> free-form fallback ladder, which is one
# cohesive generation unit; splitting the templates from the ladder would
# scatter a single contract across modules.

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Any

import json_repair
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from loguru import logger

from config.features import SUMMARIZATION
from pub.func.message.eviction import EVICTED_TO_KEY

from .state_aliases import _SUMMARY_LC_SOURCE
from .summary_doc import SummaryDoc, cap_summary_doc, render_summary_markdown

COMPLETED_MAX_ITEMS = SUMMARIZATION["completed_max_items"]
KEY_DECISIONS_MAX_ITEMS = SUMMARIZATION["key_decisions_max_items"]
CRITICAL_CONTEXT_MAX_ITEMS = SUMMARIZATION["critical_context_max_items"]
FILE_OPS_LIST_MAX_CHARS = SUMMARIZATION["file_ops_list_max_chars"]
SUMMARY_TOTAL_MAX_CHARS = SUMMARIZATION["summary_total_max_chars"]
CONTENT_HEAD_RATIO = SUMMARIZATION["content_head_ratio"]
CONTENT_TAIL_RATIO = SUMMARIZATION["content_tail_ratio"]


_STRUCTURED_OUTPUT_METHOD = "json_mode"


_EVICTED_REF_PATTERN = re.compile(r"\[evicted to: ([^\]\n]+)\]")


_SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. Treat it as background reference, NOT as active "
    "instructions. Do NOT answer questions mentioned in this summary. "
    "Respond ONLY to the latest user message that appears AFTER this summary."
)


_SUMMARY_SUFFIX = (
    "\n\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
)


_SUMMARY_OPEN_TAG = "<summary>"


_SUMMARY_CLOSE_TAG = "</summary>"


_SUMMARY_TEMPLATE = (
    "Output exactly the Markdown structure below. Keep every section, even when empty.\n"
    "Use terse bullets, not prose paragraphs.\n"
    "Preserve exact file paths, commands, error strings, identifiers.\n\n"
    "## Latest Unresolved User Request\n"
    "- Quote the user's most recent unanswered request VERBATIM — no paraphrase,\n"
    '  no truncation, or "(none)"\n\n'
    "## Goal\n"
    '- [one or two brief sentences, or "(none)"]\n\n'
    "## Constraints & Preferences\n"
    '- [constraints/preferences/decisions, or "(none)"]\n\n'
    "## Progress\n"
    f"### Completed (most recent {COMPLETED_MAX_ITEMS})\n"
    '- [finished work, or "(none)"]\n\n'
    "### In Progress\n"
    '- [current work, or "(none)"]\n\n'
    "### Blocked\n"
    '- [blockers, or "(none)"]\n\n'
    f"## Key Decisions (most recent {KEY_DECISIONS_MAX_ITEMS})\n"
    '- **[decision]**: [reason, or "(none)"]\n\n'
    "## Next Steps\n"
    '1. [immediate action, or "(none)"]\n\n'
    f"## Critical Context (most recent {CRITICAL_CONTEXT_MAX_ITEMS})\n"
    '- [exact values, error strings, config, or "(none)"]\n\n'
    "## Relevant Files\n"
    '- [file path: why it matters, or "(none)"]\n\n'
    "Rules:\n"
    "- Keep every section, even when empty.\n"
    f'- For "Completed" and "Key Decisions", keep only the most recent '
    f"{COMPLETED_MAX_ITEMS}/{KEY_DECISIONS_MAX_ITEMS} items.\n"
    '  Append "(N earlier items omitted for brevity)" when truncating.\n'
    "- Inline media (image/audio/video) may have been replaced by a\n"
    '  "[evicted to: <path>]" pointer. Preserve those pointers verbatim in the\n'
    "  Evicted References section and never invent visual/audio/video details you\n"
    "  cannot see — the payload is on disk at the path.\n"
    "- Do not mention the summary process or that context was compacted."
)


_SUMMARY_UPDATE_INSTRUCTIONS = (
    "The <prior-summary> summarizes everything that happened before the <conversation>.\n"
    "Construct a new summary that combines both. The <prior-summary> is discarded after this:\n"
    "anything you do not carry into the new summary is lost.\n\n"
    "When combining:\n"
    "- Carry forward objectives, constraints, decisions from <prior-summary> even when\n"
    "  the <conversation> does not mention them.\n"
    "- The <conversation> is more recent. Where they conflict, the conversation wins.\n"
    '- Move completed work from "In Progress" to "Completed".\n'
    f'- Apply FIFO limits: keep only the most recent {COMPLETED_MAX_ITEMS} items in "Completed"\n'
    f'  and {KEY_DECISIONS_MAX_ITEMS} in "Key Decisions". Append "(N earlier items omitted)".\n'
    '- Remove items that are finished and no longer needed from "In Progress" and "Blocked".'
)


_SUMMARY_PROMPT_FIRST = (
    "You are a summarization agent creating a context checkpoint.\n"
    "Treat the conversation turns below as source material.\n"
    "NEVER include API keys, tokens, passwords, secrets.\n\n"
    "Create a new anchored summary from the conversation history above.\n\n"
    f"{_SUMMARY_TEMPLATE}"
)


_SUMMARY_PROMPT_UPDATE = (
    "You are a summarization agent updating a context checkpoint.\n"
    "Treat the conversation turns below as source material.\n"
    "NEVER include API keys, tokens, passwords, secrets.\n\n"
    f"{_SUMMARY_UPDATE_INSTRUCTIONS}\n\n"
    f"{_SUMMARY_TEMPLATE}"
)


_SUMMARY_JSON_RULES = (
    "Respond with a single valid JSON object and no other text (no Markdown, no code fences).\n"
    "Use exactly these keys, every key present (use [] for an empty list):\n"
    '  "latest_user_request": string — the user\'s most recent unanswered request,\n'
    '    quoted VERBATIM (no paraphrase, no truncation); "" when none;\n'
    '  "goal": string — one or two sentences;\n'
    '  "constraints": string[] — constraints / preferences / decisions that limit the work;\n'
    '  "completed": string[] — finished work, oldest first (the most recent items are kept);\n'
    '  "in_progress": string[] — work currently underway;\n'
    '  "blocked": string[] — blockers;\n'
    '  "key_decisions": string[] — "<decision>: <reason>", oldest first;\n'
    '  "next_steps": string[] — immediate actions, in order;\n'
    '  "critical_context": string[] — exact values, error strings, configs that must survive;\n'
    '  "relevant_files": string[] — "<path>: why it matters";\n'
    '  "active_plan_notes": string[] — plan-scoped lessons learned while executing the\n'
    '    active plan, one line each ("symptom -> avoidance"); copy previous entries\n'
    "    forward VERBATIM and append only newly learned ones; [] when the prompt has\n"
    '    no "Active Plan (authoritative)" block;\n'
    '  "evicted_refs": string[] — the "[evicted to: <path>]" pointers, including\n'
    "    pointers that replaced offloaded inline media; carry existing entries forward.\n"
    "Media pointers: an inline image/audio/video may appear as\n"
    '"[evicted to: <path>]". Preserve those pointers verbatim in evicted_refs.\n'
    "Never describe or invent visual, audio or video details you cannot see —\n"
    "the payload is on disk at the path and can be retrieved with read_file or\n"
    "the matching media skill.\n"
    "Preserve exact file paths, commands, error strings and identifiers."
)


_SUMMARY_UPDATE_INSTRUCTIONS_STRUCTURED = (
    "The <prior-summary-json> (or legacy <prior-summary> text) summarizes everything\n"
    "that happened before the <conversation>. Construct a new JSON document that\n"
    "combines both. Anything you do not carry into the new document is lost.\n\n"
    "When combining:\n"
    "- Carry forward goal, constraints and key_decisions even when the <conversation>\n"
    "  does not mention them.\n"
    "- The <conversation> is more recent. Where they conflict, the conversation wins.\n"
    '- Move completed work from "in_progress" into "completed".\n'
    f"- Keep completed / key_decisions to the most recent {COMPLETED_MAX_ITEMS} / "
    f"{KEY_DECISIONS_MAX_ITEMS} items\n"
    "  (older items may be dropped; the pipeline caps them anyway).\n"
    '- Remove items that are finished and no longer needed from "in_progress" and "blocked".\n'
    "- Carry active_plan_notes entries forward verbatim — never rewrite an existing\n"
    "  entry — and append newly learned plan-scoped lessons at the end."
)


_SUMMARY_PROMPT_FIRST_STRUCTURED = (
    "You are a summarization agent creating a context checkpoint.\n"
    "Treat the conversation turns below as source material.\n"
    "NEVER include API keys, tokens, passwords, secrets.\n\n"
    "Create a new anchored summary from the conversation history above.\n\n"
    f"{_SUMMARY_JSON_RULES}"
)


_SUMMARY_PROMPT_UPDATE_STRUCTURED = (
    "You are a summarization agent updating a context checkpoint.\n"
    "Treat the conversation turns below as source material.\n"
    "NEVER include API keys, tokens, passwords, secrets.\n\n"
    f"{_SUMMARY_UPDATE_INSTRUCTIONS_STRUCTURED}\n\n"
    f"{_SUMMARY_JSON_RULES}"
)


def _serialize_for_summary(messages: list[AnyMessage]) -> str:
    lines: list[str] = []
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, HumanMessage):
            text = content[:2000] if len(content) > 2000 else content
            lines.append(f"[User]: {text}")
        elif isinstance(msg, AIMessage):
            if content.strip():
                lines.append(f"[Assistant]: {content[:2000]}")
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name", "")
                args_str = str(tc.get("args", ""))
                if len(args_str) > 500:
                    head = args_str[:300]
                    tail = args_str[-150:]
                    omitted = len(args_str) - len(head) - len(tail)
                    args = f"{head}...[args truncated, omitted {omitted} chars]...{tail}"
                else:
                    args = args_str
                lines.append(f"[Assistant tool call]: {name}({args})")
        elif isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", "")
            status = getattr(msg, "status", "")
            output = content
            if len(output) > 2000:
                output = output[:1800] + f"...[truncated {len(output) - 1800} chars]..."
            if status == "error":
                lines.append(f"[Tool error] ({tc_id}): {output}")
            else:
                lines.append(f"[Tool result] ({tc_id}): {output}")
    return "\n\n".join(lines)


def _filter_summary_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Filter out previous summary messages to avoid chain summary redundancy.

    Removes messages tagged with ``lc_source='summarization'`` — the
    HumanMessage ("What did we do so far?") + AIMessage (summary content)
    pair produced by ``_build_new_messages``. The previous summary text is
    already extracted via ``_extract_previous_summary`` and injected into
    ``<prior-summary>`` separately; keeping it in the serialized
    ``<conversation>`` causes token waste and summarizer confusion.

    Mirrors opencode-dev's ``hidden`` set filtering and deepagents'
    ``_filter_summary_messages``.
    """
    return [
        m
        for m in messages
        if getattr(m, "additional_kwargs", {}).get("lc_source") != _SUMMARY_LC_SOURCE
    ]


def _collect_evicted_refs(messages: Sequence[AnyMessage]) -> list[str]:
    """Collect deduplicated ``[evicted to: <path>]`` pointers, in appearance order."""
    refs: list[str] = []
    for msg in messages:
        tagged = getattr(msg, "additional_kwargs", {}).get(EVICTED_TO_KEY)
        if isinstance(tagged, str) and tagged and tagged not in refs:
            refs.append(tagged)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        for match in _EVICTED_REF_PATTERN.finditer(content):
            path = match.group(1).strip()
            if path and path not in refs:
                refs.append(path)
    return refs


def _latest_human_eviction_ref(messages: Sequence[AnyMessage]) -> str:
    """Eviction pointer of the newest user-request human message, if any.

    The field that describes the latest user request carries the request's own
    eviction pointer, so the summary chain never loses the way back to the
    verbatim text on disk. Summary artifacts (``lc_source="summarization"``)
    and internal injections (``metadata.internal`` — task-intent steering,
    subagent completion carriers) are skipped; the first real user message is
    the latest request — when it is untagged, older evictions do not belong to
    this field.
    """
    for msg in reversed(messages):
        if not isinstance(msg, HumanMessage):
            continue
        kwargs = getattr(msg, "additional_kwargs", {}) or {}
        if kwargs.get("lc_source") == _SUMMARY_LC_SOURCE:
            continue
        meta = getattr(msg, "metadata", {}) or {}
        if kwargs.get("internal") or meta.get("internal"):
            continue
        tagged = kwargs.get(EVICTED_TO_KEY)
        if isinstance(tagged, str) and tagged.strip():
            return tagged.strip()
        return ""
    return ""


def _build_static_fallback_summary(messages: list[AnyMessage]) -> str:
    user_requests: list[str] = []
    completed_actions: list[str] = []
    decisions: list[str] = []
    key_files: set[str] = set()
    errors: list[str] = []

    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, HumanMessage) and content.strip():
            user_requests.append(content[:500])
        elif isinstance(msg, AIMessage):
            if content.strip():
                lower = content.lower()
                if any(kw in lower for kw in ("decided", "choosing", "because", "therefore")):
                    decisions.append(content[:300])
                else:
                    completed_actions.append(content[:300])
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name", "")
                args_str = str(tc.get("args", ""))
                completed_actions.append(f"- {name}({args_str[:200]})")
                for word in args_str.replace("'", " ").replace('"', " ").split():
                    cleaned = word.strip("'\".,;:()[]{}")
                    if (
                        "/" in cleaned
                        or "\\" in cleaned
                        or cleaned.endswith((".py", ".md", ".js", ".ts", ".json"))
                    ):
                        if len(cleaned) > 2 and not cleaned.startswith(("http", "//")):
                            key_files.add(cleaned)
        elif isinstance(msg, ToolMessage):
            if getattr(msg, "status", "") == "error":
                errors.append(content[:300])

    parts: list[str] = [
        "## Latest Unresolved User Request",
        f"- {user_requests[-1]}" if user_requests else "- (none)",
        "",
        "## Goal",
        f"- {user_requests[0][:200]}" if user_requests else "- (unknown)",
        "",
        "## Constraints & Preferences",
        "- (none)",
        "",
        f"### Completed (most recent {COMPLETED_MAX_ITEMS})",
    ]
    for action in completed_actions[-COMPLETED_MAX_ITEMS:]:
        parts.append(f"- {action}")
    if len(completed_actions) > COMPLETED_MAX_ITEMS:
        parts.append(
            f"({len(completed_actions) - COMPLETED_MAX_ITEMS} earlier completed actions omitted for brevity)"
        )
    parts.extend(
        [
            "",
            "### In Progress",
            "- (continue previous work)",
            "",
            "### Blocked",
            f"- {errors[-1]}" if errors else "- (none)",
            "",
            f"## Key Decisions (most recent {KEY_DECISIONS_MAX_ITEMS})",
        ]
    )
    for d in decisions[-KEY_DECISIONS_MAX_ITEMS:]:
        parts.append(f"- {d}")
    parts.extend(
        [
            "",
            "## Next Steps",
            "1. (continue previous work)",
            "",
            f"## Critical Context (most recent {CRITICAL_CONTEXT_MAX_ITEMS})",
        ]
    )
    for e in errors[-CRITICAL_CONTEXT_MAX_ITEMS:]:
        parts.append(f"- {e}")
    parts.extend(["", "## Relevant Files"])
    for f in list(key_files)[:10]:
        parts.append(f"- {f}")
    if not key_files:
        parts.append("- (none)")

    return "\n".join(parts)


def _extract_file_operations(messages: list[AnyMessage]) -> dict[str, list[str]]:
    read_files: set[str] = set()
    modified_files: set[str] = set()

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                name = tc.get("name", "")
                args_str = str(tc.get("args", ""))
                paths: set[str] = set()
                for word in args_str.replace("'", " ").replace('"', " ").replace(",", " ").split():
                    cleaned = word.strip("'\".,;:()[]{}")
                    if (
                        "/" in cleaned
                        or "\\" in cleaned
                        or cleaned.endswith(
                            (
                                ".py",
                                ".md",
                                ".js",
                                ".ts",
                                ".tsx",
                                ".json",
                                ".yaml",
                                ".yml",
                                ".toml",
                                ".cfg",
                            )
                        )
                    ):
                        if len(cleaned) > 2 and not cleaned.startswith(("http", "//")):
                            paths.add(cleaned)
                if name in (
                    "read_file",
                    "read",
                    "cat",
                    "view",
                    "edit",
                    "write_file",
                    "write",
                    "patch_file",
                    "create_file",
                ):
                    if name in ("write_file", "write", "patch_file", "edit", "create_file"):
                        modified_files.update(paths)
                        read_files.update(paths)
                    else:
                        read_files.update(paths)

    read_only = read_files - modified_files
    return {
        "read_files": sorted(read_only),
        "modified_files": sorted(modified_files),
    }


def _format_file_ops(file_ops: dict[str, list[str]], previous: dict | None = None) -> str:
    if previous:
        prev_read = set(previous.get("read_files", []))
        prev_mod = set(previous.get("modified_files", []))
        all_modified = prev_mod | set(file_ops.get("modified_files", []))
        all_read = (prev_read | set(file_ops.get("read_files", []))) - all_modified
    else:
        all_read = set(file_ops.get("read_files", []))
        all_modified = set(file_ops.get("modified_files", []))

    def _fmt(files: set[str], max_chars: int) -> str:
        lines = [f"- {f}" for f in sorted(files)]
        total = sum(len(line) for line in lines)
        while total > max_chars and lines:
            dropped = lines.pop(0)
            total -= len(dropped)
        if not lines and files:
            lines.append(f"- (file list truncated, {len(files)} files)")
        return "\n".join(lines)

    read_section = _fmt(all_read, FILE_OPS_LIST_MAX_CHARS)
    mod_section = _fmt(all_modified, FILE_OPS_LIST_MAX_CHARS)

    result = "<read-files>\n"
    result += read_section if read_section else "- (none)"
    result += "\n</read-files>\n"
    result += "<modified-files>\n"
    result += mod_section if mod_section else "- (none)"
    result += "\n</modified-files>"
    return result


def _parse_file_ops_from_summary(summary_text: str) -> dict | None:
    read_match = re.search(r"<read-files>\n?(.*?)\n?</read-files>", summary_text, re.DOTALL)
    mod_match = re.search(r"<modified-files>\n?(.*?)\n?</modified-files>", summary_text, re.DOTALL)
    if not read_match and not mod_match:
        return None
    read_files = [
        line.strip("- ").strip()
        for line in (read_match.group(1) if read_match else "").split("\n")
        if line.strip().startswith("-")
    ]
    mod_files = [
        line.strip("- ").strip()
        for line in (mod_match.group(1) if mod_match else "").split("\n")
        if line.strip().startswith("-")
    ]
    return {"read_files": read_files, "modified_files": mod_files}


def _labeled_file_ops(file_ops_section: str) -> list[str]:
    parsed = _parse_file_ops_from_summary(file_ops_section) or {}
    entries: list[str] = []
    for label, key in (("read", "read_files"), ("modified", "modified_files")):
        for path in parsed.get(key, []):
            if not path or path.startswith("("):
                continue
            entry = f"{path} ({label})"
            if entry not in entries:
                entries.append(entry)
    return entries


class SummaryGenerationMixin:
    """Prompt assembly, structured/free-form summary creation, output pair."""

    def _extract_previous_doc(self, messages: list[AnyMessage]) -> SummaryDoc | None:
        """Return the newest chained SummaryDoc, or None for legacy/free-form summaries."""
        for msg in reversed(messages):
            if (
                isinstance(msg, AIMessage)
                and getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE
            ):
                raw = msg.additional_kwargs.get("summary_doc")
                if isinstance(raw, dict):
                    try:
                        return SummaryDoc.model_validate(raw)
                    except Exception:
                        logger.warning(
                            "chained summary_doc payload invalid; falling back to markdown extraction"
                        )
                return None
        return None

    def _extract_previous_summary(self, messages: list[AnyMessage]) -> str | None:
        previous_doc = self._extract_previous_doc(messages)
        if previous_doc is not None:
            return render_summary_markdown(previous_doc)
        for msg in reversed(messages):
            if (
                isinstance(msg, AIMessage)
                and getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE
            ):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if _SUMMARY_CLOSE_TAG in content:
                    start = content.find(_SUMMARY_OPEN_TAG)
                    end = content.find(_SUMMARY_CLOSE_TAG)
                    if start >= 0 and end > start:
                        return content[start + len(_SUMMARY_OPEN_TAG) : end].strip()
                return content
        for msg in reversed(messages):
            if (
                isinstance(msg, HumanMessage)
                and getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE
            ):
                return msg.content if isinstance(msg.content, str) else str(msg.content)
        return None

    def _build_summary_prompt(
        self,
        messages_text: str,
        previous_summary: str | None,
        session_id: str = "",
    ) -> str:
        conversation = (
            f"Here is the conversation so far:\n\n<conversation>\n{messages_text}\n</conversation>"
        )
        parts = [conversation]

        if previous_summary:
            parts.append(
                f"Here is the summary of the conversation before the <conversation> above:\n\n"
                f"<prior-summary>\n{previous_summary}\n</prior-summary>"
            )
            parts.append(_SUMMARY_PROMPT_UPDATE)
        else:
            parts.append(_SUMMARY_PROMPT_FIRST)

        if session_id:
            taskflow_ctx = self._taskflow_context(session_id)
            if taskflow_ctx:
                parts.append(taskflow_ctx)
            plan_ctx = self._plan_context(session_id)
            if plan_ctx:
                parts.append(plan_ctx)

        return "\n\n".join(parts)

    def _build_structured_summary_prompt(
        self,
        messages_text: str,
        previous_doc: SummaryDoc | None,
        previous_summary: str | None,
        session_id: str = "",
    ) -> str:
        conversation = (
            f"Here is the conversation so far:\n\n<conversation>\n{messages_text}\n</conversation>"
        )
        parts = [conversation]

        if previous_doc is not None:
            prior_json = json.dumps(
                previous_doc.model_dump(), ensure_ascii=False, separators=(",", ":")
            )
            parts.append(
                "Here is the previous summary document as JSON:\n\n"
                f"<prior-summary-json>\n{prior_json}\n</prior-summary-json>"
            )
            parts.append(_SUMMARY_PROMPT_UPDATE_STRUCTURED)
        elif previous_summary:
            parts.append(
                f"Here is the summary of the conversation before the <conversation> above:\n\n"
                f"<prior-summary>\n{previous_summary}\n</prior-summary>"
            )
            parts.append(_SUMMARY_PROMPT_UPDATE_STRUCTURED)
        else:
            parts.append(_SUMMARY_PROMPT_FIRST_STRUCTURED)

        if session_id:
            taskflow_ctx = self._taskflow_context(session_id)
            if taskflow_ctx:
                parts.append(taskflow_ctx)
            plan_ctx = self._plan_context(session_id)
            if plan_ctx:
                parts.append(plan_ctx)

        return "\n\n".join(parts)

    def _structured_runnable(self):
        try:
            return self._model.with_structured_output(SummaryDoc, method=_STRUCTURED_OUTPUT_METHOD)
        except TypeError:
            try:
                return self._model.with_structured_output(SummaryDoc)
            except Exception:
                logger.debug("Structured output unavailable; using the free-form summary path")
                return None
        except Exception:
            logger.debug("Structured output unavailable; using the free-form summary path")
            return None

    def _sync_json_repair_doc(self, prompt: str, config: dict) -> SummaryDoc:
        response = self._model.invoke(prompt, config=config)
        return self._parse_json_repair_doc(response.text)

    async def _async_json_repair_doc(self, prompt: str, config: dict) -> SummaryDoc:
        response = await self._model.ainvoke(prompt, config=config)
        return self._parse_json_repair_doc(response.text)

    @staticmethod
    def _parse_json_repair_doc(text: str) -> SummaryDoc:
        parsed = json_repair.loads(text.strip())
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("json_repair returned no JSON object")
        return SummaryDoc.model_validate(parsed)

    def _finalize_summary_doc(
        self,
        doc: SummaryDoc,
        messages: Sequence[AnyMessage],
        previous_doc: SummaryDoc | None,
        session_id: str = "",
    ) -> SummaryDoc:
        """Merge the code-owned fields into the model's document.

        The model proposes, the pipeline owns:

        * ``evicted_refs`` — carried pointers + the ones observed in range;
        * ``active_plan_notes`` — entries inherited verbatim from the prior
          document plus the newly appended tail (the model never rewrites an
          existing entry); cleared when no plan is active (finished or never
          associated);
        * the ``[evicted to: <path>]`` pointer of the latest user request.

        Without a ``session_id`` only the eviction-pointer merge runs, so
        document-level unit callers keep the model's array untouched.
        """
        updates: dict[str, Any] = {}

        carried = previous_doc.evicted_refs if previous_doc is not None else []
        refs: list[str] = []
        for ref in [*carried, *_collect_evicted_refs(messages)]:
            if ref and ref not in refs:
                refs.append(ref)
        if refs != doc.evicted_refs:
            updates["evicted_refs"] = refs

        if session_id:
            if self._resolve_active_plan(session_id) is None:
                if doc.active_plan_notes:
                    updates["active_plan_notes"] = []
            else:
                carried_notes = list(previous_doc.active_plan_notes) if previous_doc else []
                appended = [
                    note for note in doc.active_plan_notes if note and note not in carried_notes
                ]
                merged = [*carried_notes, *appended]
                if merged != doc.active_plan_notes:
                    updates["active_plan_notes"] = merged

        evicted_ref = _latest_human_eviction_ref(messages)
        if (
            evicted_ref
            and doc.latest_user_request.strip()
            and "[evicted to:" not in doc.latest_user_request
        ):
            updates["latest_user_request"] = (
                f"{doc.latest_user_request}\n[evicted to: {evicted_ref}]"
            )

        if not updates:
            return doc
        return doc.model_copy(update=updates)

    def _create_summary(
        self, messages_to_summarize: list[AnyMessage], session_id: str = ""
    ) -> SummaryDoc | str:
        if not messages_to_summarize:
            return "No previous conversation history."

        previous_doc = self._extract_previous_doc(messages_to_summarize)
        previous_summary = self._extract_previous_summary(messages_to_summarize)
        filtered = _filter_summary_messages(messages_to_summarize)
        serialized = _serialize_for_summary(filtered)
        if not serialized.strip():
            return "No previous conversation history."

        config = {"metadata": {"lc_source": _SUMMARY_LC_SOURCE}}
        runnable = self._structured_runnable()
        if runnable is not None:
            prompt = self._build_structured_summary_prompt(
                serialized, previous_doc, previous_summary, session_id=session_id
            )
            try:
                doc = runnable.invoke(prompt, config=config)
                if not isinstance(doc, SummaryDoc):
                    doc = SummaryDoc.model_validate(doc)
                return self._finalize_summary_doc(
                    doc, messages_to_summarize, previous_doc, session_id=session_id
                )
            except Exception as e:
                logger.warning("Structured summary failed ({}) - retrying via json_repair", e)
            try:
                doc = self._sync_json_repair_doc(prompt, config)
                return self._finalize_summary_doc(
                    doc, messages_to_summarize, previous_doc, session_id=session_id
                )
            except Exception as e:
                logger.error("json_repair summary failed ({}) - using the free-form path", e)

        prompt = self._build_summary_prompt(serialized, previous_summary, session_id=session_id)
        try:
            response = self._model.invoke(prompt, config=config)
            summary = response.text.strip()
            if not summary or len(summary) < 50:
                logger.warning("Summary too short, using fallback")
                return _build_static_fallback_summary(filtered)
            return summary
        except Exception as e:
            logger.error("LLM summary failed: {}, using fallback", e)
            return _build_static_fallback_summary(filtered)

    async def _acreate_summary(
        self, messages_to_summarize: list[AnyMessage], session_id: str = ""
    ) -> SummaryDoc | str:
        """Async twin of _create_summary; runs on the event loop, so the
        blocking prompt builders (taskflow / plan SQLite reads) and the
        store-reading finalizer are offloaded with ``asyncio.to_thread``."""
        if not messages_to_summarize:
            return "No previous conversation history."

        previous_doc = self._extract_previous_doc(messages_to_summarize)
        previous_summary = self._extract_previous_summary(messages_to_summarize)
        filtered = _filter_summary_messages(messages_to_summarize)
        serialized = _serialize_for_summary(filtered)
        if not serialized.strip():
            return "No previous conversation history."

        config = {"metadata": {"lc_source": _SUMMARY_LC_SOURCE}}
        runnable = self._structured_runnable()
        if runnable is not None:
            prompt = await asyncio.to_thread(
                self._build_structured_summary_prompt,
                serialized,
                previous_doc,
                previous_summary,
                session_id=session_id,
            )
            try:
                doc = await runnable.ainvoke(prompt, config=config)
                if not isinstance(doc, SummaryDoc):
                    doc = SummaryDoc.model_validate(doc)
                return await asyncio.to_thread(
                    self._finalize_summary_doc,
                    doc,
                    messages_to_summarize,
                    previous_doc,
                    session_id=session_id,
                )
            except Exception as e:
                logger.warning("Structured summary failed ({}) - retrying via json_repair", e)
            try:
                doc = await self._async_json_repair_doc(prompt, config)
                return await asyncio.to_thread(
                    self._finalize_summary_doc,
                    doc,
                    messages_to_summarize,
                    previous_doc,
                    session_id=session_id,
                )
            except Exception as e:
                logger.error("json_repair summary failed ({}) - using the free-form path", e)

        prompt = await asyncio.to_thread(
            self._build_summary_prompt, serialized, previous_summary, session_id=session_id
        )
        try:
            response = await self._model.ainvoke(prompt, config=config)
            summary = response.text.strip()
            if not summary or len(summary) < 50:
                logger.warning("Summary too short, using fallback")
                return _build_static_fallback_summary(filtered)
            return summary
        except Exception as e:
            logger.error("LLM summary failed: {}, using fallback", e)
            return _build_static_fallback_summary(filtered)

    def _build_new_messages(self, summary: SummaryDoc | str) -> list[BaseMessage]:
        doc_payload: dict[str, Any] | None = None
        if isinstance(summary, SummaryDoc):
            markdown = render_summary_markdown(summary)
            doc_payload = cap_summary_doc(summary).model_dump()
        else:
            markdown = summary

        if len(markdown) > SUMMARY_TOTAL_MAX_CHARS:
            head = markdown[: int(SUMMARY_TOTAL_MAX_CHARS * CONTENT_HEAD_RATIO)]
            tail = markdown[-int(SUMMARY_TOTAL_MAX_CHARS * CONTENT_TAIL_RATIO) :]
            omitted = len(markdown) - len(head) - len(tail)
            markdown = f"{head}...[summary truncated, omitted {omitted} chars]...{tail}"

        full_content = (
            f"{_SUMMARY_PREFIX}\n\n"
            f"{_SUMMARY_OPEN_TAG}\n"
            f"{markdown}\n"
            f"{_SUMMARY_CLOSE_TAG}"
            f"{_SUMMARY_SUFFIX}"
        )

        ai_kwargs: dict[str, Any] = {"lc_source": _SUMMARY_LC_SOURCE}
        if doc_payload is not None:
            ai_kwargs["summary_doc"] = doc_payload

        return [
            HumanMessage(
                content="What did we do so far?",
                additional_kwargs={"lc_source": _SUMMARY_LC_SOURCE},
            ),
            AIMessage(
                content=full_content,
                additional_kwargs=ai_kwargs,
            ),
        ]

    @staticmethod
    def _is_empty_response(response) -> bool:
        if response is None:
            return True
        if isinstance(response, AIMessage):
            content = response.content
            if isinstance(content, str):
                return not content.strip()
            if isinstance(content, list):
                return not any(p.get("text", "").strip() for p in content if isinstance(p, dict))
        if hasattr(response, "content"):
            content = response.content
            if isinstance(content, str):
                return not content.strip()
        return False
