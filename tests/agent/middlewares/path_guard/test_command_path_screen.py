"""PathGuard screens COMMAND strings too — hard-deny floor only (P3).

terminal / python_repl carry their work in ``commands`` / ``query``, which the
path-argument gate never sees, so a shell command could name a credential file
without any gate noticing. This screen closes exactly that: it refuses a command
that POSITIVELY names a hard-denied path, and leaves every other external path to
the sandbox (the real boundary). It is explicitly a mitigation — the obfuscation
tests below document what it cannot catch.
"""

from __future__ import annotations

import pytest

from agent.middlewares.path_guard.core import PathGuard, _screen_command_paths

pytestmark = [pytest.mark.unit]


class TestCommandPathScreen:
    def test_credential_file_in_a_command_is_rejected(self):
        reason = _screen_command_paths("cat ~/.ssh/id_rsa")

        assert reason is not None and "hard deny" in reason

    def test_system_credential_file_is_rejected(self):
        assert _screen_command_paths("sudo cat /etc/shadow") is not None

    def test_quoted_credential_path_is_still_rejected(self):
        assert _screen_command_paths("cat '/etc/passwd'") is not None

    def test_ordinary_external_path_is_left_to_the_sandbox(self):
        """Only the hard floor: another user's file is the sandbox's business."""
        assert _screen_command_paths("cat /home/other/notes.txt") is None

    def test_project_relative_command_passes(self):
        assert _screen_command_paths("pytest tests/agent -q") is None

    def test_url_is_not_mistaken_for_a_path(self):
        assert _screen_command_paths("curl https://example.com/etc/passwd") is None

    def test_empty_command_is_a_noop(self):
        assert _screen_command_paths("   ") is None

    def test_a_literal_path_in_a_variable_is_still_caught(self):
        """The scan reads the TEXT: assigning the path does not hide it."""
        assert _screen_command_paths("F=~/.ssh/id_rsa; cat $F") is not None

    def test_documented_gap_encoded_command(self):
        """Mitigation, not a barrier: this is why the sandbox is the real gate.

        The path never appears literally, so no text screen can see it — only
        the OS sandbox (or a Landlock-style kernel barrier) contains this.
        """
        assert _screen_command_paths("echo Y2F0IC9ldGMvcGFzc3dk | base64 -d | sh") is None


class TestPathGuardRoutesBothArgumentKinds:
    def _reject(self, tool_name: str, args: dict) -> object:
        from types import SimpleNamespace

        request = SimpleNamespace(
            tool_call={"name": tool_name, "args": args, "id": "c1"},
            state={"session_id": ""},
        )
        return PathGuard()._find_rejection(request)

    def test_terminal_command_is_screened(self):
        rejection = self._reject("terminal", {"commands": "cat /etc/shadow"})

        assert rejection is not None
        assert rejection.key == "commands"

    def test_terminal_command_list_is_screened(self):
        rejection = self._reject("terminal", {"commands": ["echo ok", "cat ~/.aws/credentials"]})

        assert rejection is not None and rejection.key == "commands"

    def test_python_repl_query_is_screened(self):
        rejection = self._reject("python_repl", {"query": "open('/etc/sudoers').read()"})

        assert rejection is not None and rejection.key == "query"

    def test_a_harmless_command_passes(self):
        assert self._reject("terminal", {"commands": "ls -la"}) is None
