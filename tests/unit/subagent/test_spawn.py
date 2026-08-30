import pytest
from agent.tools.subagent.spawn.depth import (
    get_subagent_depth,
    validate_spawn_depth,
    validate_concurrent_children,
)
from agent.tools.subagent.spawn.target_policy import validate_target_policy, is_target_allowed
from agent.tools.subagent.spawn.plan import resolve_run_timeout_seconds, split_model_ref
from agent.tools.subagent.spawn.task_name import normalize_subagent_task_name
from agent.tools.subagent.spawn.system_prompt import build_subagent_system_prompt
from agent.tools.subagent.spawn.initial_message import build_subagent_initial_user_message
from agent.tools.subagent.spawn.inherited_tool_policy import (
    apply_tool_policy,
    normalize_tool_denylist,
    DEFAULT_SUBAGENT_BLOCKED_TOOLS,
)
from agent.tools.subagent.spawn.context import prepare_spawned_context
from agent.tools.subagent.spawn.attachments import (
    validate_attachment_name,
    sanitize_mount_path,
    decode_attachment_content,
    materialize_subagent_attachments,
    AttachmentError,
)
from agent.tools.subagent.types.spawn import ContextMode
from agent.tools.subagent.types.capability import SubagentSessionRole


class TestDepth:
    def test_main_session_depth_0(self):
        assert get_subagent_depth("agent:main:session:abc") == 0

    def test_subagent_depth_1(self):
        assert get_subagent_depth("agent:main:subagent:abc") == 1

    def test_validate_spawn_depth_ok(self):
        ok, _ = validate_spawn_depth(0)
        assert ok

    def test_validate_spawn_depth_exceeded(self):
        from agent.tools.subagent.config import get_config

        ok, reason = validate_spawn_depth(get_config().max_spawn_depth)
        assert not ok
        assert "exceeds" in reason

    def test_validate_concurrent_children_ok(self):
        ok, _ = validate_concurrent_children(0)
        assert ok

    def test_validate_concurrent_children_exceeded(self):
        from agent.tools.subagent.config import get_config

        ok, reason = validate_concurrent_children(get_config().max_children_per_agent)
        assert not ok


class TestTargetPolicy:
    def test_wildcard_allowed(self):
        ok, _ = is_target_allowed("anything", ["*"])
        assert ok

    def test_explicit_allowed(self):
        ok, _ = is_target_allowed("main", ["main", "helper"])
        assert ok

    def test_not_allowed(self):
        ok, reason = is_target_allowed("unknown", ["main"])
        assert not ok

    def test_self_spawn_allowed(self):
        ok, _ = validate_target_policy("main", "main")
        assert ok


class TestPlan:
    def test_default_timeout(self):
        assert resolve_run_timeout_seconds() == 300.0

    def test_custom_timeout(self):
        assert resolve_run_timeout_seconds(600.0) == 600.0

    def test_invalid_timeout_uses_default(self):
        assert resolve_run_timeout_seconds(-1) == 300.0

    def test_split_model_ref_with_provider(self):
        provider, model = split_model_ref("openai/gpt-4")
        assert provider == "openai"
        assert model == "gpt-4"

    def test_split_model_ref_without_provider(self):
        provider, model = split_model_ref("gpt-4")
        assert provider is None
        assert model == "gpt-4"

    def test_split_model_ref_none(self):
        provider, model = split_model_ref(None)
        assert provider is None
        assert model is None


class TestTaskName:
    def test_normalize_valid(self):
        assert normalize_subagent_task_name("build_project") == "build_project"

    def test_normalize_spaces(self):
        result = normalize_subagent_task_name("build project")
        assert " " not in result

    def test_normalize_none(self):
        assert normalize_subagent_task_name(None) is None

    def test_normalize_empty(self):
        assert normalize_subagent_task_name("") is None

    def test_normalize_special_chars(self):
        result = normalize_subagent_task_name("task@#$%name")
        assert result is not None
        assert "@" not in result


class TestSystemPrompt:
    def test_leaf_role(self):
        prompt = build_subagent_system_prompt(SubagentSessionRole.LEAF, "Do X")
        assert "LEAF" in prompt
        assert "CANNOT spawn" in prompt

    def test_orchestrator_role(self):
        prompt = build_subagent_system_prompt(SubagentSessionRole.ORCHESTRATOR, "Do X")
        assert "ORCHESTRATOR" in prompt
        assert "sessions_spawn" in prompt

    def test_main_role(self):
        prompt = build_subagent_system_prompt(SubagentSessionRole.MAIN, "Do X")
        assert "subagent" in prompt


class TestInitialMessage:
    def test_without_context(self):
        msg = build_subagent_initial_user_message("Build the project")
        assert "Build the project" in msg

    def test_with_context(self):
        msg = build_subagent_initial_user_message("Build", context="Use Python")
        assert "Context" in msg
        assert "Use Python" in msg


class TestInheritedToolPolicy:
    def test_default_blocked_tools(self):
        assert "sessions_spawn" in DEFAULT_SUBAGENT_BLOCKED_TOOLS
        assert "sessions_yield" in DEFAULT_SUBAGENT_BLOCKED_TOOLS

    def test_normalize_denylist(self):
        result = normalize_tool_denylist(["a", "b", "a", "c"])
        assert result == ["a", "b", "c"]

    def test_apply_tool_policy(self):
        class FakeTool:
            def __init__(self, name):
                self.name = name

        tools = [FakeTool("read"), FakeTool("write"), FakeTool("sessions_spawn")]
        # tool_deny=None means "no policy provided" → default-blocked fallback applies
        result = apply_tool_policy(tools, [], None)
        names = [t.name for t in result]
        assert "sessions_spawn" not in names
        assert "read" in names

    def test_apply_tool_policy_with_allowlist(self):
        class FakeTool:
            def __init__(self, name):
                self.name = name

        tools = [FakeTool("read"), FakeTool("write")]
        result = apply_tool_policy(tools, ["read"], [])
        assert len(result) == 1
        assert result[0].name == "read"

    def test_explicit_deny_is_authoritative_unblocks_spawn_for_orchestrator(self):
        """Regression: ORCHESTRATOR passes an explicit deny-list omitting
        spawn/yield so it can recurse. An explicit tool_deny must be
        authoritative and NOT have default_blocked re-applied on top."""

        class FakeTool:
            def __init__(self, name):
                self.name = name

        tools = [
            FakeTool("read"),
            FakeTool("sessions_spawn"),
            FakeTool("sessions_yield"),
            FakeTool("sessions_send"),
        ]
        # Mirrors core.py ORCHESTRATOR branch: after slimming, DEFAULT minus
        # spawn/yield is an EMPTY deny-list — it must stay authoritative
        # (fallback must not re-add spawn/yield). The other high-risk tools
        # (memory, skill_manage, sessions_kill, sessions_steer) are blocked
        # via metadata scope tags, covered by test_inherited_tool_policy.py.
        orchestrator_deny = [
            t
            for t in DEFAULT_SUBAGENT_BLOCKED_TOOLS
            if t not in ("sessions_spawn", "sessions_yield")
        ]
        result = apply_tool_policy(tools, [], orchestrator_deny)
        names = [t.name for t in result]
        assert "sessions_spawn" in names  # unblocked → recursion allowed
        assert "sessions_yield" in names  # unblocked → can yield
        assert "sessions_send" in names  # unaffected bidirectional comm

    def test_none_deny_falls_back_to_default_blocked(self):
        """When no deny policy is provided (None), default-blocked tools are applied."""

        class FakeTool:
            def __init__(self, name):
                self.name = name

        tools = [FakeTool("read"), FakeTool("sessions_spawn"), FakeTool("sessions_yield")]
        result = apply_tool_policy(tools, [], None)
        names = [t.name for t in result]
        assert "sessions_spawn" not in names
        assert "sessions_yield" not in names
        assert "read" in names


class TestSpawnContext:
    @pytest.mark.asyncio
    async def test_isolated(self):
        result = await prepare_spawned_context(ContextMode.ISOLATED, None)
        assert result == []

    @pytest.mark.asyncio
    async def test_fork_without_messages(self):
        result = await prepare_spawned_context(ContextMode.FORK, None)
        assert result == []


class TestAttachmentValidation:
    def test_valid_name(self):
        assert validate_attachment_name("data.csv") == "data.csv"

    def test_reject_slash(self):
        with pytest.raises(AttachmentError):
            validate_attachment_name("path/to/file.txt")

    def test_reject_dot_dot(self):
        with pytest.raises(AttachmentError):
            validate_attachment_name("..")

    def test_reject_null(self):
        with pytest.raises(AttachmentError):
            validate_attachment_name("file\0.txt")

    def test_reject_manifest(self):
        with pytest.raises(AttachmentError):
            validate_attachment_name(".manifest.json")

    def test_reject_empty(self):
        with pytest.raises(AttachmentError):
            validate_attachment_name("")


class TestMountPathSanitization:
    def test_none(self):
        assert sanitize_mount_path(None) is None

    def test_valid_path(self):
        assert sanitize_mount_path("data/files") == "data/files"

    def test_reject_dot_dot(self):
        with pytest.raises(AttachmentError):
            sanitize_mount_path("../etc")

    def test_reject_special_chars(self):
        with pytest.raises(AttachmentError):
            sanitize_mount_path("data@evil")

    def test_strip_leading_slash(self):
        assert sanitize_mount_path("/workspace") == "workspace"

    @pytest.mark.parametrize(
        "bad",
        [
            "a/..",  # traversal nested inside a valid-looking path
            "a/../b",  # traversal in middle of path
            "....",  # '..' as substring of a longer run of dots
            "a/....",  # '..' nested inside dots + slash
            "../",  # trailing slash after traversal
            "a/../../b",  # multi-level traversal
            "sub dir",  # forbidden space
            "a\\b",  # backslash separator (runs on win32, forbid anyway)
        ],
    )
    def test_reject_all_traversal_and_forbidden(self, bad):
        # These must ALL be rejected: mount_path must never resolve outside abs_dir.
        with pytest.raises(AttachmentError):
            sanitize_mount_path(bad)

    def test_dot_resolves_to_attack_dir_root(self):
        # A lone '.' collapses to the attachment dir root itself (safe, confined)
        assert sanitize_mount_path(".") == "."


class TestDecodeContent:
    def test_utf8(self):
        result = decode_attachment_content("hello", "utf8")
        assert result == b"hello"

    def test_base64(self):
        result = decode_attachment_content("aGVsbG8=", "base64")
        assert result == b"hello"

    def test_invalid_base64(self):
        with pytest.raises(AttachmentError):
            decode_attachment_content("not-valid-base64!!!", "base64")


class TestMaterializeAttachments:
    @pytest.mark.asyncio
    async def test_no_attachments(self):
        result = await materialize_subagent_attachments(None)
        assert result.status == "ok"
        assert result.abs_dir is None

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path):
        attachments = [{"name": "test.txt", "content": "hello world"}]
        result = await materialize_subagent_attachments(attachments, child_workspace=tmp_path)
        assert result.status == "ok"
        assert result.abs_dir is not None
        assert ".openclaw" in result.abs_dir and "attachments" in result.abs_dir
        assert "Attachments:" in result.system_prompt_suffix
        import json
        from pathlib import Path

        manifest = json.loads((Path(result.abs_dir) / ".manifest.json").read_text())
        assert len(manifest["files"]) == 1
        assert manifest["files"][0]["name"] == "test.txt"

    @pytest.mark.asyncio
    async def test_too_many_files(self, tmp_path):
        attachments = [{"name": f"f{i}.txt", "content": "x"} for i in range(3)]
        result = await materialize_subagent_attachments(
            attachments, child_workspace=tmp_path, max_files=2
        )
        assert result.status == "error"
        assert "Too many" in result.error

    @pytest.mark.asyncio
    async def test_file_too_large(self, tmp_path):
        attachments = [{"name": "big.txt", "content": "x" * 200}]
        result = await materialize_subagent_attachments(
            attachments, child_workspace=tmp_path, max_file_bytes=100
        )
        assert result.status == "error"
        assert "exceeds max file size" in result.error

    @pytest.mark.asyncio
    async def test_total_too_large(self, tmp_path):
        attachments = [
            {"name": "a.txt", "content": "x" * 60},
            {"name": "b.txt", "content": "y" * 60},
        ]
        result = await materialize_subagent_attachments(
            attachments, child_workspace=tmp_path, max_total_bytes=100
        )
        assert result.status == "error"
        assert "Total" in result.error

    @pytest.mark.asyncio
    async def test_base64_attachment(self, tmp_path):
        import base64

        encoded = base64.b64encode(b"binary data").decode()
        attachments = [{"name": "data.bin", "content": encoded, "encoding": "base64"}]
        result = await materialize_subagent_attachments(attachments, child_workspace=tmp_path)
        assert result.status == "ok"
        from pathlib import Path

        content = (Path(result.abs_dir) / "data.bin").read_bytes()
        assert content == b"binary data"

    @pytest.mark.asyncio
    async def test_mount_path(self, tmp_path):
        attachments = [{"name": "f.txt", "content": "hi", "mount_path": "sub/dir"}]
        result = await materialize_subagent_attachments(attachments, child_workspace=tmp_path)
        assert result.status == "ok"
        from pathlib import Path

        assert (Path(result.abs_dir) / "sub" / "dir" / "f.txt").exists()

    @pytest.mark.asyncio
    async def test_reject_path_traversal_name(self, tmp_path):
        attachments = [{"name": "../evil.txt", "content": "hack"}]
        result = await materialize_subagent_attachments(attachments, child_workspace=tmp_path)
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_reject_mount_path_traversal(self, tmp_path):
        # Even a mount_path nested inside a valid-looking path must not escape.
        attachments = [{"name": "evil.txt", "content": "hack", "mount_path": "sub/.."}]
        result = await materialize_subagent_attachments(attachments, child_workspace=tmp_path)
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_no_file_leaks_outside_attack_dir(self, tmp_path):
        from pathlib import Path

        # Mount path '.' collapses to the attack dir root; the file must land
        # inside ab_dir and nowhere else in the workspace.
        marker = tmp_path / "MARKER"
        marker.write_text("keep")
        attachments = [{"name": "f.txt", "content": "hi", "mount_path": "."}]
        result = await materialize_subagent_attachments(attachments, child_workspace=tmp_path)
        assert result.status == "ok"
        assert result.abs_dir is not None
        abs_dir = Path(result.abs_dir)
        assert (abs_dir / "f.txt").exists()
        # f.txt must not exist anywhere else under the workspace
        leaks = [str(p) for p in tmp_path.rglob("f.txt") if abs_dir not in p.parents]
        assert leaks == []

    @pytest.mark.asyncio
    async def test_prompt_suffix_has_rel_dir_and_bytes(self, tmp_path):
        from pathlib import Path

        attachments = [
            {"name": "a.txt", "content": "hello"},
            {"name": "b.txt", "content": "world !"},
        ]
        result = await materialize_subagent_attachments(attachments, child_workspace=tmp_path)
        assert result.status == "ok"
        suffix = result.system_prompt_suffix
        # byte count matches decoded content lengths (5 + 7 = 12)
        assert "2 file(s)" in suffix
        assert "12 bytes" in suffix
        # rel_dir must be referenced so the child knows where files live
        assert result.abs_dir is not None
        assert result.rel_dir is not None
        assert result.rel_dir in suffix
        assert result.rel_dir.startswith(".openclaw/attachments/")
        # the dir actually exists on disk
        assert Path(result.abs_dir).is_dir()


class TestSpawnSubagentDirect:
    """端到端测试 spawn_subagent_direct 主流程（mock 重依赖）"""

    @pytest.fixture(autouse=True)
    def _cleanup_registry(self):
        from agent.tools.subagent.registry import clear as clear_registry

        clear_registry()
        yield
        clear_registry()

    @pytest.fixture(autouse=True)
    def _mock_execute(self):
        from unittest.mock import AsyncMock, patch

        async def fake_execute(**kwargs):
            pass

        with patch(
            "agent.tools.subagent.spawn.core._execute_subagent",
            new=AsyncMock(side_effect=fake_execute),
        ):
            yield

    @pytest.mark.asyncio
    async def test_empty_task_returns_error(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct

        result = await spawn_subagent_direct(
            task="",
            requester_session_key="agent:main:session:test",
        )
        assert result.status == "error"
        assert "required" in result.error

    @pytest.mark.asyncio
    async def test_whitespace_task_returns_error(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct

        result = await spawn_subagent_direct(
            task="   ",
            requester_session_key="agent:main:session:test",
        )
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_basic_spawn_accepted(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct

        result = await spawn_subagent_direct(
            task="Do something",
            requester_session_key="agent:main:session:test",
        )
        assert result.status == "accepted"
        assert result.child_session_key is not None
        assert ":subagent:" in result.child_session_key
        assert result.run_id is not None

    @pytest.mark.asyncio
    async def test_spawn_registers_run_in_registry(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct
        from agent.tools.subagent.registry import get_run

        result = await spawn_subagent_direct(
            task="Do something",
            requester_session_key="agent:main:session:test",
        )
        assert result.status == "accepted"
        run = get_run(result.run_id)
        assert run is not None
        assert run.task == "Do something"
        assert run.depth == 1
        assert run.requester_session_key == "agent:main:session:test"

    @pytest.mark.asyncio
    async def test_spawn_with_task_name(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct
        from agent.tools.subagent.registry import get_run

        result = await spawn_subagent_direct(
            task="Do something",
            requester_session_key="agent:main:session:test",
            task_name="my_task",
        )
        assert result.status == "accepted"
        run = get_run(result.run_id)
        assert run.task_name == "my_task"

    @pytest.mark.asyncio
    async def test_spawn_with_label_and_thinking(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct
        from agent.tools.subagent.registry import get_run

        result = await spawn_subagent_direct(
            task="Do something",
            requester_session_key="agent:main:session:test",
            label="build",
            thinking="high",
        )
        assert result.status == "accepted"
        run = get_run(result.run_id)
        assert run.label == "build"
        assert run.thinking == "high"

    @pytest.mark.asyncio
    async def test_spawn_depth_tracker(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct
        from agent.tools.subagent.registry import get_run

        parent_key = "agent:main:session:depth_test"
        result = await spawn_subagent_direct(
            task="Task 1",
            requester_session_key=parent_key,
        )
        assert result.status == "accepted"
        run = get_run(result.run_id)
        assert run.depth == 1

    @pytest.mark.asyncio
    async def test_spawn_exceeds_max_depth(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct

        deep_key = "agent:main:subagent:a:subagent:b:subagent:c"
        result = await spawn_subagent_direct(
            task="Too deep",
            requester_session_key=deep_key,
        )
        assert result.status == "forbidden"
        assert "exceeds" in result.error

    @pytest.mark.asyncio
    async def test_spawn_orchestrator_removes_subagent_tools(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct
        from agent.tools.subagent.registry import get_run

        result = await spawn_subagent_direct(
            task="Do something",
            requester_session_key="agent:main:session:test",
        )
        assert result.status == "accepted"
        run = get_run(result.run_id)
        assert run.role.name == "ORCHESTRATOR"
        assert "sessions_spawn" not in run.inherited_tool_deny
        assert "sessions_yield" not in run.inherited_tool_deny

    @pytest.mark.asyncio
    async def test_to_result_dict(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct

        result = await spawn_subagent_direct(
            task="Do something",
            requester_session_key="agent:main:session:test",
        )
        d = result.to_dict()
        assert d["status"] == "accepted"
        assert d["child_session_key"] == result.child_session_key
        assert d["run_id"] == result.run_id
        assert d["error"] is None

    @pytest.mark.asyncio
    async def test_error_result_to_dict(self):
        from agent.tools.subagent.spawn.core import SpawnResult

        r = SpawnResult(status="error", error="something went wrong")
        d = r.to_dict()
        assert d["status"] == "error"
        assert d["error"] == "something went wrong"
        assert d["child_session_key"] is None
        assert d["run_id"] is None

    @pytest.mark.asyncio
    async def test_fork_context_mode(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct
        from agent.tools.subagent.registry import get_run
        from agent.tools.subagent.types.spawn import ContextMode

        result = await spawn_subagent_direct(
            task="Do something",
            requester_session_key="agent:main:session:test",
            context=ContextMode.FORK,
        )
        assert result.status == "accepted"
        run = get_run(result.run_id)
        assert run.context_mode == ContextMode.FORK

    @pytest.mark.asyncio
    async def test_concurrent_children_limit(self):
        from agent.tools.subagent.spawn.core import spawn_subagent_direct
        from agent.tools.subagent.config import get_config, set_config
        from agent.tools.subagent.registry import clear as clear_registry

        orig_config = get_config()
        try:
            limited_config = get_config()
            limited_config = limited_config.model_copy(update={"max_children_per_agent": 2})
            set_config(limited_config)
            clear_registry()

            from agent.tools.subagent.registry import register_run
            from agent.tools.subagent.types.registry import ExecutionStatus

            for i in range(2):
                fake_run = register_run(
                    child_session_key=f"agent:main:subagent:fake{i}",
                    requester_session_key="agent:main:session:limit_test",
                    task=f"Fake task {i}",
                    depth=1,
                )
                fake_run.execution.status = ExecutionStatus.RUNNING

            result = await spawn_subagent_direct(
                task="Blocked task",
                requester_session_key="agent:main:session:limit_test",
            )
            assert result.status == "forbidden"
            assert "Concurrent" in result.error
        finally:
            set_config(orig_config)
            clear_registry()
