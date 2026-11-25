"""Regression lock: intent-drift checking is ADDITIVE to the original verify prompt.

The original verify contract — reviewer report schema, failed-todo reopening,
and the ``verified: true`` completion rule — must remain byte-for-byte present.
The intent check only appends guidance; it must never replace or tighten the
existing prompt (e.g. by requiring a new field that an older reviewer report
would not contain).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]

_ORIGINAL_REPORT_SCHEMA = (
    '{"verified": bool, "results": [{"todo_id": N, "passed": bool, '
    '"evidence": "...", "suggested_fix": "..."}]}'
)
_ORIGINAL_COMPLETION_RULE = (
    'Only a {"verified": true} report may complete this todo; the transition barrier\n'
    "  requires a linked, finished reviewer."
)
_ORIGINAL_FAILED_TODO_RULE = (
    "For every passed=false todo_id, patch that todo back to pending and put the\n"
    "  evidence in its notes. Do not rewrite the whole list; passed todos stay completed."
)
_ORIGINAL_FINAL_CHECK_RULE = (
    "- Read what you changed (`git diff` / the edited files): confirm it matches the\n"
    "  request, with no stubs, TODOs, placeholders or unrequested scope creep."
)


class TestSystemPromptUntouched:
    def test_intent_anchor_is_not_part_of_the_system_prompt(self):
        """The anchor rides the reminder layer; the cached system prompt is unchanged."""
        from workspace.prompt_builder import build_system_prompt

        assert "Intent Anchor" not in build_system_prompt()


class TestVerifyDirectiveOriginalContract:
    def test_original_report_schema_is_unchanged(self):
        from agent.middlewares.todo_continuation.core import _VERIFY_DIRECTIVE

        assert _ORIGINAL_REPORT_SCHEMA in _VERIFY_DIRECTIVE

    def test_original_completion_rule_is_unchanged(self):
        from agent.middlewares.todo_continuation.core import _VERIFY_DIRECTIVE

        assert _ORIGINAL_COMPLETION_RULE in _VERIFY_DIRECTIVE

    def test_original_failed_todo_rule_is_unchanged(self):
        from agent.middlewares.todo_continuation.core import _VERIFY_DIRECTIVE

        assert _ORIGINAL_FAILED_TODO_RULE in _VERIFY_DIRECTIVE

    def test_intent_guidance_is_appended_not_substituted(self):
        from agent.middlewares.todo_continuation.core import _VERIFY_DIRECTIVE

        assert _VERIFY_DIRECTIVE.count("ADDITIONALLY") >= 3
        # The new optional fields are described, not made mandatory for
        # completion: the old {"verified": true} rule still stands above.
        assert '"intent_match": false' in _VERIFY_DIRECTIVE
        assert '"intent_deviations"' in _VERIFY_DIRECTIVE
        assert '"verified": true' in _VERIFY_DIRECTIVE

    def test_final_verification_reminder_keeps_all_original_checks(self):
        from agent.middlewares.todo_continuation.core import _VERIFICATION_REMINDER

        assert _ORIGINAL_FINAL_CHECK_RULE in _VERIFICATION_REMINDER
        assert (
            "- Actually run the checks (tests / typecheck / lint) through the terminal tool."
            in _VERIFICATION_REMINDER
        )
        assert '"Probably works" = NOT verified.' in _VERIFICATION_REMINDER
        assert "Intent Anchor" in _VERIFICATION_REMINDER  # the appended check


class TestVerifyTodoTextOriginalContract:
    @pytest.mark.parametrize(
        "lang,original",
        [
            (
                "en",
                "[VERIFY] Run the plan's acceptance checks with a reviewer subagent: spawn "
                'sessions_spawn(functional_role="reviewer"), patch this todo\'s subagent_id '
                "with the returned child_session_key, then re-open only the todos it reports "
                "as failed.",
            ),
            (
                "zh",
                "[VERIFY] 用 reviewer 子代理对整套计划跑验收：spawn "
                'sessions_spawn(functional_role="reviewer")，把返回的 child_session_key '
                "写进本 todo 的 subagent_id，然后只把报告为失败的 todo 回退重开。",
            ),
            (
                "ja",
                "[VERIFY] reviewer サブエージェントで計画全体の受け入れ検証を行う："
                'sessions_spawn(functional_role="reviewer") を実行し、返された '
                "child_session_key をこの todo の subagent_id に書き、失敗と報告された "
                "todo だけを再オープンする。",
            ),
            (
                "ko",
                "[VERIFY] reviewer 서브에이전트로 계획 전체의 인수 검증을 실행한다: "
                'sessions_spawn(functional_role="reviewer")를 호출하고 반환된 '
                "child_session_key를 이 todo의 subagent_id에 기록한 뒤, 실패로 보고된 "
                "todo만 다시 연다.",
            ),
        ],
    )
    def test_original_verify_todo_text_is_a_prefix(self, lang, original):
        from agent.tools.todolist.service import _VERIFY_BASE_BY_LANG

        assert _VERIFY_BASE_BY_LANG[lang].startswith(original)
