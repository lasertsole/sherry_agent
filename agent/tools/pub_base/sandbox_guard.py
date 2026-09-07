"""Sandbox guard mixin (audit 1.1.6)."""

from langchain_core.tools import ToolException

from agent.tools.pub_base.sandbox import SandboxPolicy, read_policy


class SandboxGuardMixin:
    """Mixin for tools that accept a ``sandbox`` flag and enforce
    sandbox policy. Subclasses must provide a ``metadata`` attribute
    (inherited from ``langchain_core.tools.BaseTool``)."""

    metadata: dict  # provided by BaseTool

    def _deny_sandbox_bypass(self, sandbox: bool) -> None:
        """Guard sandbox=False calls: subagents are denied, REQUIRED denies all.

        Matrix cells (agent/tools/pub_base/sandbox.py docstring):
        - required + sandbox=False → DENIED (no approval path until Task 8)
        - subagent scope + sandbox=False → DENIED (bypass is a main-session,
          human-approved decision only)
        - sandbox=True is never gated here.
        """
        if sandbox:
            return
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        scope = metadata.get("caller_scope", "main")
        if scope != "main":
            raise ToolException(f"沙箱绕过仅限主会话人工审批；当前 scope={scope}")
        if read_policy() is SandboxPolicy.REQUIRED:
            raise ToolException(
                "SANDBOX_POLICY=required 拒绝未沙箱执行：sandbox=False 需要主会话（main）人工审批"
            )
