"""Sandbox guard mixin."""

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
        - required + sandbox=False → DENIED (no approval path)
        - subagent scope + sandbox=False → DENIED (bypass is a main-session,
          human-approved decision only)
        - sandbox=True is never gated here.
        """
        if sandbox:
            return
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        scope = metadata.get("caller_scope", "main")
        if scope != "main":
            raise ToolException(
                f"Sandbox bypass requires main-session human approval; current scope={scope}"
            )
        if read_policy() is SandboxPolicy.REQUIRED:
            raise ToolException(
                "SANDBOX_POLICY=required denied non-sandboxed execution: "
                "sandbox=False requires manual approval from the main session"
            )

    def _deny_unsandboxed_degrade(self, sandbox: bool) -> None:
        """Refuse an AUTO degrade when the caller has no approval path.

        ``SANDBOX_POLICY=auto`` + no usable OS backend normally degrades to an
        unsandboxed run; the humanInTheLoop middleware raises the approval card
        for a MAIN turn, and headless main turns auto-deny it. A subagent or
        background caller has no such middleware, so it must refuse instead of
        silently falling back to the regex blacklist (the fail-closed half of
        the Windows/no-bwrap gap). ``OFF`` is an explicit operator choice and
        ``REQUIRED`` already raises from ``get_backend``.
        """
        if not sandbox or read_policy() is not SandboxPolicy.AUTO:
            return
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        scope = metadata.get("caller_scope", "main")
        if scope == "main":
            return
        from agent.tools.pub_base.sandbox import sandbox_effective

        if sandbox_effective(True):
            return
        raise ToolException(
            "Sandboxed execution refused: SANDBOX_POLICY=auto and no OS sandbox backend is "
            f"available on this host, while caller_scope={scope} has no operator approval path. "
            "Install bubblewrap (Linux) or use macOS Seatbelt; set SANDBOX_POLICY=off only as a "
            "deliberate operator choice."
        )
