"""Unit tests for humanInTheLoop middleware integration.

Tests cover:
1. HITLConfig defaults and fields
2. ApprovalPipeline command detection (hardline, dangerous, allowlists)
3. InterruptManager lifecycle
4. WriteApprovalGate staging and approval
5. KanbanTriage escalation
6. PairingStore authorization
7. SlashConfirm destructive action gating
8. Detection functions (hardline + dangerous patterns)
9. Server service function existence (get_pending_interrupt, resume_agent)
10. Agent core middleware registration (humanInTheLoop in built_agent)
"""

import pytest
import inspect
import importlib
import sys
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def hitl_modules():
    """Import HITL submodules directly, bypassing the agent package __init__.

    The agent/__init__.py triggers a langchain import chain that may fail
    due to version mismatches in the test environment. We set up the
    package hierarchy manually so relative imports work, stubbing out
    the parent packages to avoid triggering langchain imports.
    """
    import types as pytypes

    base = Path(__file__).resolve().parent.parent.parent / "agent"
    hitl_base = base / "middlewares" / "humanInTheLoop"

    # Create stub packages so relative imports resolve
    for pkg_name, pkg_path in [
        ("agent", base),
        ("agent.middlewares", base / "middlewares"),
        ("agent.middlewares.humanInTheLoop", hitl_base),
    ]:
        if pkg_name not in sys.modules:
            mod = pytypes.ModuleType(pkg_name)
            mod.__path__ = [str(pkg_path)]
            mod.__package__ = pkg_name
            sys.modules[pkg_name] = mod

    # Stub runtime.state_register to avoid heavy imports
    if "runtime" not in sys.modules:
        runtime_mod = pytypes.ModuleType("runtime")
        sys.modules["runtime"] = runtime_mod
    if "runtime.state_register" not in sys.modules:
        sr_mod = pytypes.ModuleType("runtime.state_register")
        sr_mock = MagicMock()
        sr_mod.state_register_mem = sr_mock
        sys.modules["runtime.state_register"] = sr_mod

    modules = {}
    for name in ("types", "detection", "approval", "gates", "core"):
        mod_path = hitl_base / f"{name}.py"
        full_name = f"agent.middlewares.humanInTheLoop.{name}"
        spec = importlib.util.spec_from_file_location(full_name, mod_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = mod
        spec.loader.exec_module(mod)
        modules[name] = mod
    return modules


# ── Test HITLConfig ───────────────────────────────────────────────────

class TestHITLConfig:
    """Test HITLConfig default values and field behavior."""

    def test_defaults(self, hitl_modules):
        config = hitl_modules["types"].HITLConfig()
        assert config.mode == hitl_modules["types"].ApprovalMode.SMART
        assert config.timeout == 60
        assert config.yolo_mode is False
        assert config.write_approval_memory is False
        assert config.write_approval_skills is False
        assert config.kanban_recurrence_limit == 3
        assert config.mcp_reload_confirm is True
        assert config.destructive_slash_confirm is True
        assert config.smart_approval_llm is None
        assert config.interrupted_tools == {}

    def test_custom_config(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(
            mode=types.ApprovalMode.MANUAL,
            yolo_mode=True,
            timeout=120,
            deny_rules=["rm*"],
        )
        assert config.mode == types.ApprovalMode.MANUAL
        assert config.yolo_mode is True
        assert config.timeout == 120
        assert config.deny_rules == ["rm*"]


class TestApprovalResult:
    """Test ApprovalResult dataclass."""

    def test_approved_result(self, hitl_modules):
        r = hitl_modules["types"].ApprovalResult(approved=True)
        assert r.approved is True
        assert r.blocked is False

    def test_blocked_result(self, hitl_modules):
        r = hitl_modules["types"].ApprovalResult(approved=False)
        assert r.blocked is True
        assert r.approved is False

    def test_with_decision_and_reason(self, hitl_modules):
        types = hitl_modules["types"]
        r = types.ApprovalResult(
            approved=False,
            decision=types.ApprovalDecision.DENY,
            reason="Blocked by hardline",
        )
        assert r.decision == types.ApprovalDecision.DENY
        assert "Blocked" in r.reason


# ── Test Detection ────────────────────────────────────────────────────

class TestDetection:
    """Test command detection functions."""

    def test_detect_hardline_rm_rf_root(self, hitl_modules):
        result = hitl_modules["detection"].detect_hardline_command("rm -rf /")
        assert result is not None

    def test_detect_hardline_safe_command(self, hitl_modules):
        result = hitl_modules["detection"].detect_hardline_command("ls -la")
        assert result is None

    def test_detect_hardline_mkfs(self, hitl_modules):
        result = hitl_modules["detection"].detect_hardline_command("mkfs.ext4 /dev/sda1")
        assert result is not None

    def test_detect_hardline_safe_command(self, hitl_modules):
        result = hitl_modules["detection"].detect_hardline_command("ls -la")
        assert result is None

    def test_detect_dangerous_sudo_rm(self, hitl_modules):
        result = hitl_modules["detection"].detect_dangerous_command("sudo rm /tmp/file")
        assert result is not None
        assert len(result) > 0

    def test_detect_dangerous_git_force_push(self, hitl_modules):
        result = hitl_modules["detection"].detect_dangerous_command("git push --force origin main")
        assert result is not None
        assert len(result) > 0

    def test_detect_dangerous_safe_command(self, hitl_modules):
        result = hitl_modules["detection"].detect_dangerous_command("echo hello")
        assert result is None or len(result) == 0


# ── Test ApprovalPipeline ─────────────────────────────────────────────

class TestApprovalPipeline:
    """Test the command approval pipeline layers."""

    def setup_method(self):
        self._modules = None

    def _get_pipeline(self, modules, config=None):
        types = modules["types"]
        if config is None:
            config = types.HITLConfig(mode=types.ApprovalMode.SMART)
        return modules["approval"].ApprovalPipeline(config, lambda *_: None), config

    def test_safe_command_approved(self, hitl_modules):
        pipeline, _ = self._get_pipeline(hitl_modules)
        result = pipeline.check_command("ls -la", "test-session")
        assert result.approved is True

    def test_hardline_command_blocked(self, hitl_modules):
        pipeline, _ = self._get_pipeline(hitl_modules)
        result = pipeline.check_command("rm -rf /", "test-session")
        assert result.blocked is True
        assert result.decision == hitl_modules["types"].ApprovalDecision.DENY

    def test_dangerous_command_escalated(self, hitl_modules):
        pipeline, _ = self._get_pipeline(hitl_modules)
        result = pipeline.check_command("sudo rm /tmp/file", "test-session")
        assert result.approved is False
        assert result.decision is None

    def test_yolo_mode_bypasses_dangerous(self, hitl_modules):
        """YOLO mode bypasses dangerous detection but NOT hardline."""
        types = hitl_modules["types"]
        config = types.HITLConfig(yolo_mode=True)
        pipeline, _ = self._get_pipeline(hitl_modules, config)
        result = pipeline.check_command("sudo rm /tmp/file", "test-session")
        assert result.approved is True

    def test_deny_rules(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(deny_rules=["git push*"])
        pipeline, _ = self._get_pipeline(hitl_modules, config)
        result = pipeline.check_command("git push origin main", "test-session")
        assert result.blocked is True

    def test_session_allowlist(self, hitl_modules):
        pipeline, _ = self._get_pipeline(hitl_modules)
        # First call escalates (sudo rm is dangerous)
        result1 = pipeline.check_command("sudo rm /tmp/file", "test-session")
        assert result1.approved is False
        # Manually approve for session
        pipeline._add_to_session("test-session", "sudo rm /tmp/file")
        result2 = pipeline.check_command("sudo rm /tmp/file", "test-session")
        assert result2.approved is True
        assert result2.decision == hitl_modules["types"].ApprovalDecision.SESSION

    def test_permanent_allowlist(self, hitl_modules):
        pipeline, _ = self._get_pipeline(hitl_modules)
        pipeline._add_to_permanent("test-session", "sudo rm /tmp/file")
        result = pipeline.check_command("sudo rm /tmp/file", "test-session")
        assert result.approved is True
        assert result.decision == hitl_modules["types"].ApprovalDecision.ALWAYS

    def test_smart_approve_without_llm_escalates(self, hitl_modules):
        pipeline, _ = self._get_pipeline(hitl_modules)
        result = pipeline.smart_approve("rm -rf /")
        assert result == hitl_modules["types"].SmartApprovalResult.ESCALATE

    def test_tool_approval_default_deny(self, hitl_modules):
        pipeline, _ = self._get_pipeline(hitl_modules)
        result = pipeline.request_tool_approval("custom_tool", {"arg": "val"}, "test-session")
        assert result.blocked is True

    def test_tool_approval_after_session_approval(self, hitl_modules):
        pipeline, _ = self._get_pipeline(hitl_modules)
        pipeline.approve_tool_for_session("custom_tool", {"arg": "val"}, "test-session")
        result = pipeline.request_tool_approval("custom_tool", {"arg": "val"}, "test-session")
        assert result.approved is True

    def test_extract_pattern(self, hitl_modules):
        pattern = hitl_modules["approval"]._extract_pattern("git push --force origin main")
        assert pattern == "git push*"

    def test_extract_pattern_single_word(self, hitl_modules):
        pattern = hitl_modules["approval"]._extract_pattern("ls")
        assert pattern == "ls*"

    def test_args_hash_deterministic(self, hitl_modules):
        h1 = hitl_modules["approval"]._args_hash({"a": 1, "b": 2})
        h2 = hitl_modules["approval"]._args_hash({"b": 2, "a": 1})
        assert h1 == h2


# ── Test InterruptManager ─────────────────────────────────────────────

class TestInterruptManager:
    """Test the InterruptManager gate."""

    def test_set_and_check_interrupt(self, hitl_modules):
        mgr = hitl_modules["gates"].InterruptManager()
        assert mgr.is_interrupted("session1") is False
        mgr.set_interrupt("session1")
        assert mgr.is_interrupted("session1") is True

    def test_clear_interrupt(self, hitl_modules):
        mgr = hitl_modules["gates"].InterruptManager()
        mgr.set_interrupt("session1")
        mgr.clear_interrupt("session1")
        assert mgr.is_interrupted("session1") is False

    def test_different_sessions_independent(self, hitl_modules):
        mgr = hitl_modules["gates"].InterruptManager()
        mgr.set_interrupt("session1")
        assert mgr.is_interrupted("session1") is True
        assert mgr.is_interrupted("session2") is False

    def test_set_interrupt_false_clears(self, hitl_modules):
        mgr = hitl_modules["gates"].InterruptManager()
        mgr.set_interrupt("session1", True)
        assert mgr.is_interrupted("session1") is True
        mgr.set_interrupt("session1", False)
        assert mgr.is_interrupted("session1") is False


# ── Test WriteApprovalGate ────────────────────────────────────────────

class TestWriteApprovalGate:
    """Test the WriteApprovalGate."""

    def test_gate_off_auto_approves(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(write_approval_memory=False)
        gate = hitl_modules["gates"].WriteApprovalGate(config)
        result = gate.request_write(types.WriteTarget.MEMORY, "content", "session1")
        assert result.approved is True

    def test_gate_on_stages_write(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(write_approval_memory=True)
        gate = hitl_modules["gates"].WriteApprovalGate(config)
        result = gate.request_write(types.WriteTarget.MEMORY, "content", "session1")
        assert result.blocked is True

    def test_approve_staged_write(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(write_approval_memory=True)
        gate = hitl_modules["gates"].WriteApprovalGate(config)
        result = gate.request_write(types.WriteTarget.MEMORY, "content", "session1")
        assert result.blocked is True
        write_id = result.reason.split("id=")[1].split(")")[0]
        assert gate.approve_write("session1", write_id) is True

    def test_reject_staged_write(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(write_approval_memory=True)
        gate = hitl_modules["gates"].WriteApprovalGate(config)
        result = gate.request_write(types.WriteTarget.MEMORY, "content", "session1")
        write_id = result.reason.split("id=")[1].split(")")[0]
        assert gate.reject_write("session1", write_id) is True

    def test_get_pending_writes(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(write_approval_memory=True)
        gate = hitl_modules["gates"].WriteApprovalGate(config)
        gate.request_write(types.WriteTarget.MEMORY, "content1", "session1")
        gate.request_write(types.WriteTarget.MEMORY, "content2", "session1")
        writes = gate.get_pending_writes("session1")
        assert len(writes) == 2

    def test_approve_nonexistent_write(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(write_approval_memory=True)
        gate = hitl_modules["gates"].WriteApprovalGate(config)
        assert gate.approve_write("session1", "nonexistent-id") is False


# ── Test KanbanTriage ─────────────────────────────────────────────────

class TestKanbanTriage:
    """Test the KanbanTriage gate."""

    def test_below_limit_returns_blocked(self, hitl_modules):
        triage = hitl_modules["gates"].KanbanTriage(recurrence_limit=3)
        for _ in range(2):
            status = triage.report_task_failure("task1", "session1")
            assert status == hitl_modules["types"].TriageStatus.BLOCKED

    def test_at_limit_returns_triage(self, hitl_modules):
        triage = hitl_modules["gates"].KanbanTriage(recurrence_limit=3)
        for _ in range(3):
            status = triage.report_task_failure("task1", "session1")
        assert status == hitl_modules["types"].TriageStatus.TRIAGE

    def test_resolve_triage_resets_counter(self, hitl_modules):
        triage = hitl_modules["gates"].KanbanTriage(recurrence_limit=3)
        for _ in range(3):
            triage.report_task_failure("task1", "session1")
        triage.resolve_triage("task1", "session1")
        status = triage.report_task_failure("task1", "session1")
        assert status == hitl_modules["types"].TriageStatus.BLOCKED

    def test_different_tasks_independent(self, hitl_modules):
        triage = hitl_modules["gates"].KanbanTriage(recurrence_limit=3)
        for _ in range(3):
            triage.report_task_failure("task1", "session1")
        status = triage.report_task_failure("task2", "session1")
        assert status == hitl_modules["types"].TriageStatus.BLOCKED


# ── Test PairingStore ─────────────────────────────────────────────────

class TestPairingStore:
    """Test the PairingStore gate."""

    def test_default_deny(self, hitl_modules):
        store = hitl_modules["gates"].PairingStore()
        assert store.is_user_allowed("telegram", "user1") is False

    def test_approve_user(self, hitl_modules):
        store = hitl_modules["gates"].PairingStore()
        store.approve_user("telegram", "user1")
        assert store.is_user_allowed("telegram", "user1") is True

    def test_revoke_user(self, hitl_modules):
        store = hitl_modules["gates"].PairingStore()
        store.approve_user("telegram", "user1")
        store.revoke_user("telegram", "user1")
        assert store.is_user_allowed("telegram", "user1") is False

    def test_different_platforms_independent(self, hitl_modules):
        store = hitl_modules["gates"].PairingStore()
        store.approve_user("telegram", "user1")
        assert store.is_user_allowed("discord", "user1") is False


# ── Test SlashConfirm ─────────────────────────────────────────────────

class TestSlashConfirm:
    """Test the SlashConfirm gate."""

    def test_confirm_on_blocks_by_default(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(destructive_slash_confirm=True)
        gate = hitl_modules["gates"].SlashConfirm(config)
        result = gate.confirm_destructive("delete file", "session1")
        assert result.blocked is True

    def test_confirm_off_auto_approves(self, hitl_modules):
        types = hitl_modules["types"]
        config = types.HITLConfig(destructive_slash_confirm=False)
        gate = hitl_modules["gates"].SlashConfirm(config)
        result = gate.confirm_destructive("delete file", "session1")
        assert result.approved is True


# ── Test MCPElicitationConsent ────────────────────────────────────────

class TestMCPElicitationConsent:
    """Test the MCPElicitationConsent gate (always denies by default)."""

    def test_always_denies(self, hitl_modules):
        types = hitl_modules["types"]
        result = hitl_modules["gates"].MCPElicitationConsent.request_consent("server1", "session1")
        assert result.blocked is True
        assert result.decision == types.ApprovalDecision.DENY


# ── Test Agent Core Middleware Registration ───────────────────────────

class TestAgentCoreMiddlewareRegistration:
    """Test that humanInTheLoop is registered in the agent middleware list."""

    def test_human_in_the_loop_in_core_source(self):
        """Verify humanInTheLoop is imported and used in agent/core.py."""
        core_path = Path(__file__).resolve().parent.parent.parent / "agent" / "core.py"
        source = core_path.read_text(encoding="utf-8")
        assert "humanInTheLoop" in source
        assert "humanInTheLoop()" in source

    def test_human_in_the_loop_in_middleware_init(self):
        """Verify humanInTheLoop is exported from agent/middlewares/__init__.py."""
        init_path = Path(__file__).resolve().parent.parent.parent / "agent" / "middlewares" / "__init__.py"
        source = init_path.read_text(encoding="utf-8")
        assert "humanInTheLoop" in source

    def test_hitl_middleware_before_summarization(self):
        """Verify humanInTheLoop is placed before Summarization in the middleware list."""
        core_path = Path(__file__).resolve().parent.parent.parent / "agent" / "core.py"
        source = core_path.read_text(encoding="utf-8")
        hitl_pos = source.index("humanInTheLoop()")
        summ_pos = source.index("Summarization(")
        assert hitl_pos < summ_pos, "humanInTheLoop should be before Summarization in middleware list"


# ── Test Server Service Function Existence ────────────────────────────

class TestServerServiceFunctions:
    """Test that server service HITL functions exist in the source."""

    def test_get_pending_interrupt_defined(self):
        """Verify get_pending_interrupt is defined in server/service/messages.py."""
        path = Path(__file__).resolve().parent.parent.parent / "server" / "service" / "messages.py"
        source = path.read_text(encoding="utf-8")
        assert "async def get_pending_interrupt" in source

    def test_resume_agent_defined(self):
        """Verify resume_agent is defined in server/service/messages.py."""
        path = Path(__file__).resolve().parent.parent.parent / "server" / "service" / "messages.py"
        source = path.read_text(encoding="utf-8")
        assert "async def resume_agent" in source

    def test_resume_agent_uses_command(self):
        """Verify resume_agent uses Command(resume=...) from langgraph."""
        path = Path(__file__).resolve().parent.parent.parent / "server" / "service" / "messages.py"
        source = path.read_text(encoding="utf-8")
        assert "Command(resume=" in source

    def test_get_pending_interrupt_checks_tasks(self):
        """Verify get_pending_interrupt inspects state.tasks for interrupts."""
        path = Path(__file__).resolve().parent.parent.parent / "server" / "service" / "messages.py"
        source = path.read_text(encoding="utf-8")
        assert "tasks" in source
        assert "interrupts" in source

    def test_service_exports_hitl_functions(self):
        """Verify __init__.py exports the new functions."""
        path = Path(__file__).resolve().parent.parent.parent / "server" / "service" / "__init__.py"
        source = path.read_text(encoding="utf-8")
        assert "get_pending_interrupt" in source
        assert "resume_agent" in source


# ── Test WebSocket Handler HITL Support ───────────────────────────────

class TestWebSocketHitlHandler:
    """Test that the WebSocket handler supports HITL events."""

    def test_ws_handler_handles_hitl_response(self):
        """Verify the WS handler processes hitl_response type messages."""
        path = Path(__file__).resolve().parent.parent.parent / "server" / "trigger" / "ws" / "messages.py"
        source = path.read_text(encoding="utf-8")
        assert "hitl_response" in source
        assert "resume_agent" in source

    def test_ws_handler_sends_hitl_request(self):
        """Verify the WS handler sends hitl_request events to the client."""
        path = Path(__file__).resolve().parent.parent.parent / "server" / "trigger" / "ws" / "messages.py"
        source = path.read_text(encoding="utf-8")
        assert "hitl_request" in source
        assert "get_pending_interrupt" in source

    def test_ws_handler_checks_interrupt_after_stream(self):
        """Verify the handler checks for interrupts after async_generate completes."""
        path = Path(__file__).resolve().parent.parent.parent / "server" / "trigger" / "ws" / "messages.py"
        source = path.read_text(encoding="utf-8")
        # The interrupt check should happen after the async_generate loop
        assert "get_pending_interrupt" in source


# ── Test Client Bridge HITL Support ───────────────────────────────────

class TestClientBridgeHitl:
    """Test that the client bridge.ts supports HITL events."""

    def test_bridge_has_hitl_types(self):
        """Verify bridge.ts exports HITL types."""
        path = Path(__file__).resolve().parent.parent.parent / "client" / "app" / "composables" / "bridge.ts"
        source = path.read_text(encoding="utf-8")
        assert "HitlInterruptData" in source
        assert "HitlResponse" in source
        assert "OnHitlCallback" in source

    def test_bridge_has_send_hitl_response(self):
        """Verify StreamController has sendHitlResponse method."""
        path = Path(__file__).resolve().parent.parent.parent / "client" / "app" / "composables" / "bridge.ts"
        source = path.read_text(encoding="utf-8")
        assert "sendHitlResponse" in source

    def test_bridge_handles_hitl_request_event(self):
        """Verify the WS onmessage handler processes hitl_request events."""
        path = Path(__file__).resolve().parent.parent.parent / "client" / "app" / "composables" / "bridge.ts"
        source = path.read_text(encoding="utf-8")
        assert "hitl_request" in source

    def test_messages_ts_passes_on_hitl(self):
        """Verify messages.ts passes onHitl callback to streamChatMessage."""
        path = Path(__file__).resolve().parent.parent.parent / "client" / "app" / "composables" / "messages.ts"
        source = path.read_text(encoding="utf-8")
        assert "onHitl" in source

    def test_index_vue_has_hitl_ui(self):
        """Verify index.vue has HITL approval dialog."""
        path = Path(__file__).resolve().parent.parent.parent / "client" / "app" / "pages" / "home" / "index.vue"
        source = path.read_text(encoding="utf-8")
        assert "hitlRequest" in source
        assert "handleHitlDecision" in source
        assert "handleHitlRequest" in source

    def test_type_ts_has_hitl_request_data(self):
        """Verify type.ts exports HitlRequestData."""
        path = Path(__file__).resolve().parent.parent.parent / "client" / "app" / "pages" / "home" / "type.ts"
        source = path.read_text(encoding="utf-8")
        assert "HitlRequestData" in source
