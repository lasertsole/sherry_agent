"""Dispatch equivalence for ``SkillManage._run`` (action registry).

Each action must route to its primitive with the exact argument shape, and each
validation failure must return the exact error string unchanged. The registry
keys must stay in lockstep with the tool's ``action`` schema Literal.
"""

import json
from typing import Any, get_args

import pytest

from agent.tools.skill_tools import skill_manage as sm

pytestmark = [pytest.mark.unit]


class _Recorder:
    """Returns a non-success result so the telemetry path is skipped."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def make(self, tag: str):
        def _handler(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((tag, args, kwargs))
            return {"success": False, "routed": tag}

        return _handler


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    for primitive, tag in (
        ("_create_skill", "create"),
        ("_edit_skill", "edit"),
        ("_patch_skill", "patch"),
        ("_delete_skill", "delete"),
        ("_write_file", "write_file"),
        ("_remove_file", "remove_file"),
    ):
        monkeypatch.setattr(sm, primitive, rec.make(tag))
    return rec


def _run(action: str, **overrides: Any) -> Any:
    args: dict[str, Any] = {
        "action": action,
        "name": "demo",
        "content": None,
        "old_string": None,
        "new_string": None,
        "replace_all": None,
        "category": None,
        "file_path": None,
        "file_content": None,
        "absorbed_into": None,
    }
    args.update(overrides)
    return sm.SkillManage()._run(**args)


def test_registry_keys_match_schema_literal() -> None:
    schema_actions = set(get_args(sm.SkillManageSchema.model_fields["action"].annotation))
    assert set(sm._SKILL_ACTION_HANDLERS) == schema_actions


def test_create_routes_and_validates(recorder: _Recorder) -> None:
    out = json.loads(_run("create", content="body", category="cat"))
    assert out == {"success": False, "routed": "create"}
    assert recorder.calls == [("create", ("demo", "body", "cat"), {})]

    assert _run("create") == (
        "content is required for 'create'. Provide the full SKILL.md text (frontmatter + body)."
    )


def test_edit_routes_and_validates(recorder: _Recorder) -> None:
    out = json.loads(_run("edit", content="updated"))
    assert out == {"success": False, "routed": "edit"}
    assert recorder.calls == [("edit", ("demo", "updated"), {})]

    assert _run("edit") == (
        "content is required for 'edit'. Provide the full updated SKILL.md text."
    )


def test_patch_routes_and_validates(recorder: _Recorder) -> None:
    out = json.loads(_run("patch", old_string="a", new_string="", file_path="f", replace_all=True))
    assert out == {"success": False, "routed": "patch"}
    assert recorder.calls == [("patch", ("demo", "a", "", "f", True), {})]

    assert _run("patch") == "old_string is required for 'patch'. Provide the text to find."
    assert _run("patch", old_string="a") == (
        "new_string is required for 'patch'. Use empty string to delete matched text."
    )


def test_delete_routes_with_absorbed_into(recorder: _Recorder) -> None:
    out = json.loads(_run("delete", absorbed_into="umbrella"))
    assert out == {"success": False, "routed": "delete"}
    assert recorder.calls == [("delete", ("demo",), {"absorbed_into": "umbrella"})]


def test_write_file_routes_and_validates(recorder: _Recorder) -> None:
    out = json.loads(_run("write_file", file_path="references/a.md", file_content="A"))
    assert out == {"success": False, "routed": "write_file"}
    assert recorder.calls == [("write_file", ("demo", "references/a.md", "A"), {})]

    assert _run("write_file") == (
        "file_path is required for 'write_file'. Example: 'references/api-guide.md'"
    )
    assert _run("write_file", file_path="references/a.md") == (
        "file_content is required for 'write_file'."
    )


def test_remove_file_routes_and_validates(recorder: _Recorder) -> None:
    out = json.loads(_run("remove_file", file_path="references/a.md"))
    assert out == {"success": False, "routed": "remove_file"}
    assert recorder.calls == [("remove_file", ("demo", "references/a.md"), {})]

    assert _run("remove_file") == "file_path is required for 'remove_file'."


def test_unknown_action_returns_error_json(recorder: _Recorder) -> None:
    out = json.loads(_run("bogus"))
    assert out["success"] is False
    assert "Unknown action 'bogus'" in out["error"]
    assert recorder.calls == []
