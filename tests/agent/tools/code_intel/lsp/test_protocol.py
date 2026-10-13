"""Unit tests for the LSP protocol primitives: language ids, diagnostics, edits.

The extension → language → ``languageId`` chain is asserted per language because
the config key is a server handle and the protocol identifier diverges for Bash
(``bash`` → ``shellscript``) — the single most error-prone mapping in the module.
"""

from __future__ import annotations

import pytest

from agent.tools.code_intel.lsp import protocol

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_EXTENSION_CASES = {
    "a.py": ("python", "python"),
    "a.pyi": ("python", "python"),
    "a.ts": ("typescript", "typescript"),
    "a.tsx": ("typescript", "typescript"),
    "a.rs": ("rust", "rust"),
    "a.go": ("go", "go"),
    "a.c": ("cpp", "cpp"),
    "a.cpp": ("cpp", "cpp"),
    "a.cc": ("cpp", "cpp"),
    "a.cxx": ("cpp", "cpp"),
    "a.h": ("cpp", "cpp"),
    "a.hpp": ("cpp", "cpp"),
    "a.java": ("java", "java"),
    "a.rb": ("ruby", "ruby"),
    "a.rake": ("ruby", "ruby"),
    "a.sh": ("bash", "shellscript"),
    "a.bash": ("bash", "shellscript"),
    "a.zsh": ("bash", "shellscript"),
    "a.vue": ("vue", "vue"),
    "a.yaml": ("yaml", "yaml"),
    "a.yml": ("yaml", "yaml"),
}


class TestLanguageDetection:
    @pytest.mark.parametrize("file_path", sorted(_EXTENSION_CASES))
    def test_extension_maps_to_language_and_language_id(self, file_path: str) -> None:
        language, expected_id = _EXTENSION_CASES[file_path]
        assert protocol.detect_language(file_path) == language
        assert protocol.language_id(language) == expected_id

    def test_uppercase_extension_is_normalized(self) -> None:
        assert protocol.detect_language("MAIN.CPP") == "cpp"

    def test_unknown_extension_returns_none(self) -> None:
        assert protocol.detect_language("notes.txt") is None

    def test_unknown_language_id_falls_through(self) -> None:
        assert protocol.language_id("haskell") == "haskell"


class TestDiagnosticFormatting:
    def test_severity_names_and_range(self) -> None:
        formatted = protocol.format_diagnostic(
            {
                "message": "boom",
                "severity": 2,
                "source": "pyright",
                "code": "E1",
                "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 4}},
            }
        )
        assert formatted is not None
        assert formatted["severity"] == "warning"
        assert formatted["source"] == "pyright"
        assert formatted["code"] == "E1"
        assert formatted["range"]["start_line"] == 2

    def test_unknown_severity_is_reported(self) -> None:
        formatted = protocol.format_diagnostic({"message": "x"})
        assert formatted is not None
        assert formatted["severity"] == "unknown"

    def test_non_dict_returns_none(self) -> None:
        assert protocol.format_diagnostic("nope") is None  # type: ignore[arg-type]


class TestWorkspaceEditFormatting:
    def test_changes_map_is_grouped(self) -> None:
        span = {
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
            "newText": "x",
        }
        edit = {"changes": {"file:///repo/a.py": [span], "file:///repo/b.py": [span]}}
        groups = protocol.normalize_workspace_edit(edit)
        assert {group["path"] for group in groups} == {"/repo/a.py", "/repo/b.py"}

    def test_rangeless_edit_is_dropped(self) -> None:
        edit = {"changes": {"file:///repo/a.py": [{"range": None, "newText": "x"}]}}
        assert protocol.normalize_workspace_edit(edit) == []

    def test_document_changes_text_edits_are_flattened(self) -> None:
        edit = {
            "documentChanges": [
                {
                    "textDocument": {"uri": "file:///repo/a.py"},
                    "edits": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 1},
                            },
                            "newText": "a",
                        },
                        {
                            "range": {
                                "start": {"line": 1, "character": 0},
                                "end": {"line": 1, "character": 1},
                            },
                            "newText": "b",
                        },
                    ],
                }
            ]
        }
        groups = protocol.normalize_workspace_edit(edit)
        assert len(groups) == 1
        assert len(groups[0]["edits"]) == 2
        assert groups[0]["edits"][0]["new_text"] == "a"

    def test_non_dict_returns_empty(self) -> None:
        assert protocol.normalize_workspace_edit(None) == []
