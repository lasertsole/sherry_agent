"""Static safety classification and the file-aware batch plan."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools.parallel_safety import (
    BatchGroup,
    FILE_READ_TOOLS,
    FILE_WRITE_TOOLS,
    file_resource_key,
    plan_batch,
    tool_is_parallel_safe,
)

pytestmark = [pytest.mark.unit]


def _tool(metadata: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(metadata=metadata or {})


def _call(name: str, path: str | None = None, *, call_id: str = "c1") -> dict:
    args = {} if path is None else {"file_path": path}
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def test_file_sets_are_disjoint_and_cover_the_three_file_tools() -> None:
    assert FILE_READ_TOOLS == {"read_file"}
    assert FILE_WRITE_TOOLS == {"write_file", "patch_file"}
    assert not (FILE_READ_TOOLS & FILE_WRITE_TOOLS)


def test_explicit_safety_declarations() -> None:
    assert tool_is_parallel_safe("read_file", None) is True
    assert tool_is_parallel_safe("web_search", None) is True
    assert tool_is_parallel_safe("memory", _tool({"idempotent": True})) is False
    assert tool_is_parallel_safe("sessions_spawn", _tool({"concurrent_safe": True})) is True
    assert tool_is_parallel_safe("write_file", _tool({"side_effect_scope": "workspace"})) is False
    assert tool_is_parallel_safe("unknown_tool", None) is False
    assert tool_is_parallel_safe("unknown_tool", _tool({"side_effect_scope": "none"})) is True


def test_destructive_overrides_concurrent_safe() -> None:
    assert (
        tool_is_parallel_safe("danger", _tool({"destructive": True, "concurrent_safe": True}))
        is False
    )


def test_file_resource_key_uses_canonical_path_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    state = {"session_id": "s1"}

    read = file_resource_key(_call("read_file", str(path)), state)
    write = file_resource_key(_call("write_file", str(path)), state)
    patch = file_resource_key(_call("patch_file", str(path)), state)

    assert read == (str(path.resolve()), "read")
    assert write == (str(path.resolve()), "write")
    assert patch == (str(path.resolve()), "write")
    assert file_resource_key(_call("read_file", str(tmp_path / "b.txt")), state) != read
    assert file_resource_key(_call("read_file", ""), state) is None
    assert file_resource_key(_call("memory", str(path)), state) is None


def test_plan_keeps_all_reads_concurrent(tmp_path: Path) -> None:
    path = str(tmp_path / "a.txt")
    plan = plan_batch(
        [_call("read_file", path, call_id="r1"), _call("read_file", path, call_id="r2")],
        {},
        {"session_id": "s1"},
    )

    assert plan.barrier is False
    assert len(plan.groups) == 1
    assert plan.groups[0].indices == (0, 1)
    assert plan.groups[0].concurrent is True


def test_plan_orders_a_write_group_in_call_order(tmp_path: Path) -> None:
    path = str(tmp_path / "a.txt")
    plan = plan_batch(
        [
            _call("write_file", path, call_id="w1"),
            _call("read_file", path, call_id="r1"),
            _call("read_file", path, call_id="r2"),
        ],
        {},
        {"session_id": "s1"},
    )

    assert plan.barrier is False
    assert plan.groups == (BatchGroup(indices=(0, 1, 2), concurrent=False),)


def test_different_paths_are_independent(tmp_path: Path) -> None:
    a = str(tmp_path / "a.txt")
    b = str(tmp_path / "b.txt")
    plan = plan_batch(
        [_call("read_file", a, call_id="r1"), _call("write_file", b, call_id="w1")],
        {},
        {"session_id": "s1"},
    )

    assert plan.barrier is False
    assert len(plan.groups) == 2
    assert plan.groups[0].concurrent is True
    assert plan.groups[1].concurrent is False
    assert plan.groups[0].indices == (0,)
    assert plan.groups[1].indices == (1,)


def test_a_non_file_unsafe_tool_is_a_barrier(tmp_path: Path) -> None:
    path = str(tmp_path / "a.txt")
    plan = plan_batch(
        [_call("read_file", path), _call("terminal", call_id="t1")],
        {"terminal": _tool({"side_effect_scope": "system"})},
        {"session_id": "s1"},
    )

    assert plan.barrier is True
    assert plan.groups == (BatchGroup(indices=(0, 1), concurrent=False),)
    assert "terminal" in plan.reason


def test_an_unresolvable_file_path_is_a_barrier() -> None:
    plan = plan_batch(
        [_call("read_file", "")],
        {},
        {"session_id": "s1"},
    )

    assert plan.barrier is True
    assert plan.groups[0].indices == (0,)


def test_safe_calls_are_singleton_concurrent_groups() -> None:
    plan = plan_batch(
        [_call("web_search"), _call("web_fetch", call_id="w2")],
        {},
        {"session_id": "s1"},
    )

    assert plan.barrier is False
    assert all(group.concurrent for group in plan.groups)
    assert [group.indices for group in plan.groups] == [(0,), (1,)]
