"""TDD tests for audit 1.1.5/1.1.6/1.1.7 shared pub_base utilities.

* 1.1.5 ``class_or_instance_schema(parent_cls)`` — descriptor factory letting
  ``tool_call_schema`` be read from class AND instance (replaces the two
  per-tool ``_ClassOrInstanceSchema`` copies that hardcoded the parent class).
* 1.1.6 ``SandboxGuardMixin._deny_sandbox_bypass`` — shared guard replacing
  the two identical per-tool methods.
* 1.1.7 ``tool_error(message, **extra)`` — shared JSON error helper replacing
  the duplicated ``_tool_error`` in memory.py / message_search.py.
"""

import json

import pytest
from langchain_core.tools import ToolException

from agent.tools.pub_base.sandbox_guard import SandboxGuardMixin
from agent.tools.pub_base.schema_utils import class_or_instance_schema
from agent.tools.pub_base.tool_utils import tool_error

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


# ---------------------------------------------------------------------------
# 1.1.5 class_or_instance_schema
# ---------------------------------------------------------------------------


class _Parent:
    @property
    def tool_call_schema(self):  # langchain shape: instance @property
        return {"schema_of": type(self).__name__}


class _Child(_Parent):
    # real usage shape: ClassVar attribute holding the descriptor
    tool_call_schema = class_or_instance_schema(_Parent)


class TestClassOrInstanceSchema:
    def test_class_access_returns_schema(self):
        assert _Child.tool_call_schema == {"schema_of": "_Child"}

    def test_instance_access_returns_schema(self):
        assert _Child().tool_call_schema == {"schema_of": "_Child"}

    def test_parent_class_access_still_bare_property(self):
        """Parent has the langchain quirk (bare property on class access) —
        the descriptor exists to fix it on the child, not the parent."""
        import langchain_core.tools  # noqa: F401 — quirk documented in module docstring

        assert isinstance(_Parent.__dict__.get("tool_call_schema"), property)


# ---------------------------------------------------------------------------
# 1.1.6 SandboxGuardMixin
# ---------------------------------------------------------------------------


class _GuardedTool(SandboxGuardMixin):
    def __init__(self, metadata, policy=None):
        self.metadata = metadata
        self._policy = policy


class TestSandboxGuardMixin:
    def _tool(self, scope="main", policy=None):
        tool = _GuardedTool({"caller_scope": scope})
        if policy is not None:
            import agent.tools.pub_base.sandbox_guard as sg

            original = sg.read_policy
            sg.read_policy = lambda: policy
            return tool, original, sg
        return tool, None, None

    def test_sandbox_true_never_gated(self, monkeypatch):
        import agent.tools.pub_base.sandbox_guard as sg

        monkeypatch.setattr(sg, "read_policy", lambda: sg.SandboxPolicy.REQUIRED)
        tool = _GuardedTool({"caller_scope": "subagent"})
        tool._deny_sandbox_bypass(True)  # no raise

    def test_subagent_scope_denied(self):
        tool = _GuardedTool({"caller_scope": "subagent"})
        with pytest.raises(ToolException, match="Sandbox bypass requires main-session"):
            tool._deny_sandbox_bypass(False)

    def test_main_scope_allowed_under_auto(self, monkeypatch):
        import agent.tools.pub_base.sandbox_guard as sg

        monkeypatch.setattr(sg, "read_policy", lambda: sg.SandboxPolicy.AUTO)
        tool = _GuardedTool({"caller_scope": "main"})
        tool._deny_sandbox_bypass(False)  # no raise

    def test_required_policy_denies_main(self, monkeypatch):
        import agent.tools.pub_base.sandbox_guard as sg

        monkeypatch.setattr(sg, "read_policy", lambda: sg.SandboxPolicy.REQUIRED)
        tool = _GuardedTool({"caller_scope": "main"})
        with pytest.raises(ToolException, match="SANDBOX_POLICY=required"):
            tool._deny_sandbox_bypass(False)

    def test_non_dict_metadata_treated_as_main(self, monkeypatch):
        import agent.tools.pub_base.sandbox_guard as sg

        monkeypatch.setattr(sg, "read_policy", lambda: sg.SandboxPolicy.AUTO)
        tool = _GuardedTool("not-a-dict")
        tool._deny_sandbox_bypass(False)  # no raise


# ---------------------------------------------------------------------------
# 1.1.7 tool_error
# ---------------------------------------------------------------------------


class TestToolError:
    def test_basic_error(self):
        assert tool_error("file not found") == '{"error": "file not found"}'

    def test_extra_kwargs(self):
        assert tool_error("bad input", success=False) == json.dumps(
            {"error": "bad input", "success": False}, ensure_ascii=False
        )

    def test_ensure_ascii_false(self):
        assert "中文" in tool_error("中文错误")
