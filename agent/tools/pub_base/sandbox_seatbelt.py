"""macOS Seatbelt (sandbox-exec) sandbox backend.

仅验证构造逻辑，未在 macOS 实机验证（Windows 开发机限制；测试全部 mock，
不执行真实 sandbox-exec）。sbpl profile 模板与 ``sandbox-exec -p ... --``
包装结构参照 oh-my-openagent ``sandbox-platform.ts`` 的 ``buildDarwinProfile``。
"""

import json
import shutil

from agent.tools.pub_base.sandbox import SandboxBackend
from config.path import ROOT_DIR, TEMP_DIR


class SeatbeltBackend(SandboxBackend):
    """Wraps commands with ``sandbox-exec -p <profile> -- <cmd...>``.

    ``probe()`` only checks ``shutil.which("sandbox-exec")`` — the seatbelt
    binary provides no exit-code based smoke probe (unlike bwrap). The probe
    result is cached at class level (probed once per process lifetime).
    """

    _probe_result: bool | None = None

    def probe(self) -> bool:
        cls = type(self)
        if cls._probe_result is None:
            cls._probe_result = shutil.which("sandbox-exec") is not None
        return cls._probe_result

    @staticmethod
    def _build_profile() -> str:
        """Build the sbpl profile string.

        Order is load-bearing: ``(deny file-write*)`` under ``(allow default)``
        turns the sandbox into "allow everything except file writes", then
        explicit allows re-open the writable paths. Template order is the spec.
        Paths are embedded via ``json.dumps`` (sbpl strings are JSON-like) so
        quotes/backslashes in paths cannot break out into injected sbpl forms.
        """
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            f'(allow file-write* (subpath {json.dumps(str(ROOT_DIR))}))',
            f'(allow file-write* (subpath {json.dumps(str(TEMP_DIR))}))',
            '(allow file-write* (literal "/dev/null"))',
            '(allow file-write* (literal "/dev/tty"))',
        ]
        return "\n".join(lines)

    def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        profile = self._build_profile()
        return ["sandbox-exec", "-p", profile, "--", *cmd], env
