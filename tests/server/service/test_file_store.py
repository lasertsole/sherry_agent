"""TDD tests for audit 2.1.7 — shared ``FileStore`` template
(server/service/file_store.py) wired into workplace/memory/heartbeat.

Pins the shared read/validate/write semantics the three services used to
duplicate: exact validation-error wording per store, interleaved
validate+write ordering, workplace's re-validating update, memory's
merge-without-revalidation write, and heartbeat's task-text-only length
budget.
"""

import pytest

from server.service.file_store import FileStore

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

ALLOWED = ["A.md", "B.md"]


class _TmpStore(FileStore):
    def __init__(self, base, *, noun="file", max_len=100):
        self.file_names = list(ALLOWED)
        self.max_content_length = max_len
        self.error_noun = noun
        self.base = base
        self.before_read_calls = 0

    def _before_read(self) -> None:
        self.before_read_calls += 1

    def _file_path(self, file_name):
        return self.base / file_name


class TestValidation:
    def test_invalid_name_message(self, tmp_path):
        store = _TmpStore(tmp_path)
        with pytest.raises(ValueError, match="Invalid file name: nope.md"):
            store.write_files({"nope.md": "x"})

    def test_non_string_content_message(self, tmp_path):
        store = _TmpStore(tmp_path)
        with pytest.raises(ValueError, match="Invalid content type for file: A.md"):
            store.write_files({"A.md": 42})

    def test_empty_content_message(self, tmp_path):
        store = _TmpStore(tmp_path)
        with pytest.raises(ValueError, match="Content is empty for file: A.md"):
            store.write_files({"A.md": "   "})

    def test_too_long_message(self, tmp_path):
        store = _TmpStore(tmp_path, max_len=5)
        with pytest.raises(ValueError, match="Content too long for file: A.md"):
            store.write_files({"A.md": "123456"})

    def test_memory_noun_wording(self, tmp_path):
        store = _TmpStore(tmp_path, noun="memory file")
        with pytest.raises(ValueError, match="Invalid memory file name: nope.md"):
            store.write_files({"nope.md": "x"})

    def test_heartbeat_noun_wording(self, tmp_path):
        store = _TmpStore(tmp_path, noun="heartbeat file", max_len=5)
        with pytest.raises(ValueError, match="Content too long for heartbeat file: A.md"):
            store.write_files({"A.md": "123456"})


class TestReadWrite:
    def test_read_returns_only_existing_files(self, tmp_path):
        (tmp_path / "A.md").write_text("alpha", encoding="utf-8")
        store = _TmpStore(tmp_path)

        assert store.read_files() == {"A.md": "alpha"}
        assert store.before_read_calls == 1

    def test_write_creates_files(self, tmp_path):
        store = _TmpStore(tmp_path)
        store.write_files({"A.md": "one", "B.md": "two"})

        assert (tmp_path / "A.md").read_text(encoding="utf-8") == "one"
        assert (tmp_path / "B.md").read_text(encoding="utf-8") == "two"

    def test_write_interleaves_validate_and_write(self, tmp_path):
        """An invalid later entry does not undo earlier writes (the original
        per-entry loops wrote each entry right after validating it)."""
        store = _TmpStore(tmp_path, max_len=5)

        with pytest.raises(ValueError, match="Content too long"):
            store.write_files({"A.md": "short", "B.md": "way too long"})

        assert (tmp_path / "A.md").read_text(encoding="utf-8") == "short"
        assert not (tmp_path / "B.md").exists()

    def test_write_leaves_other_files_unchanged(self, tmp_path):
        (tmp_path / "B.md").write_text("keep", encoding="utf-8")
        store = _TmpStore(tmp_path)

        store.write_files({"A.md": "only-a"})

        assert (tmp_path / "B.md").read_text(encoding="utf-8") == "keep"


class TestUpdateVariants:
    def test_update_merges_and_revalidates_all(self, tmp_path):
        """workplace semantics: update re-validates everything it writes back."""
        (tmp_path / "A.md").write_text("x" * 200, encoding="utf-8")  # over budget on disk
        store = _TmpStore(tmp_path, max_len=100)

        with pytest.raises(ValueError, match="Content too long for file: A.md"):
            store.update_files({"B.md": "ok"})

        # nothing written: the re-validation of A.md failed inside write_files
        assert not (tmp_path / "B.md").exists()

    def test_merge_validates_only_provided_entries(self, tmp_path):
        """memory semantics: existing over-budget content is written back as-is."""
        (tmp_path / "A.md").write_text("x" * 200, encoding="utf-8")
        store = _TmpStore(tmp_path, max_len=100)

        store.merge_files({"B.md": "ok"})

        assert (tmp_path / "B.md").read_text(encoding="utf-8") == "ok"
        assert (tmp_path / "A.md").read_text(encoding="utf-8") == "x" * 200


class TestServiceWiring:
    def test_memory_write_merges_into_existing(self, tmp_path, monkeypatch):
        from server.service import memory as memory_service

        fake_dir = tmp_path / "memory"
        monkeypatch.setattr(memory_service, "MEMORY_DIR", fake_dir)

        memory_service.write_memory_files({"MEMORY.md": "m1"})
        memory_service.write_memory_files({"USER.md": "u1"})  # does not clobber MEMORY.md

        assert (fake_dir / "MEMORY.md").read_text(encoding="utf-8") == "m1"
        assert (fake_dir / "USER.md").read_text(encoding="utf-8") == "u1"
        assert memory_service.read_memory_files() == {"MEMORY.md": "m1", "USER.md": "u1"}

    def test_memory_error_wording(self, tmp_path, monkeypatch):
        from server.service import memory as memory_service

        monkeypatch.setattr(memory_service, "MEMORY_DIR", tmp_path)
        with pytest.raises(ValueError, match="Invalid memory file name: BAD.md"):
            memory_service.write_memory_files({"BAD.md": "x"})

    def test_workplace_write_validates_and_writes(self, tmp_path, monkeypatch):
        from server.service import workplace as workplace_service

        monkeypatch.setattr(workplace_service, "WORKSPACE_DIR", tmp_path)
        monkeypatch.setattr(workplace_service, "ensure_workspace_system_files", lambda: None)

        name = workplace_service.EDITABLE_SYSTEM_FILE_NAMES[0]
        workplace_service.write_system_prompt_file({name: "content"})

        assert (tmp_path / name).read_text(encoding="utf-8") == "content"

    def test_workplace_rejects_agents_write(self, tmp_path, monkeypatch):
        """AGENTS.md is injected into the system prompt but must not be writable
        through the /system_prompt API (frontend edit chain removed; defense in depth)."""
        from server.service import workplace as workplace_service

        monkeypatch.setattr(workplace_service, "WORKSPACE_DIR", tmp_path)
        monkeypatch.setattr(workplace_service, "ensure_workspace_system_files", lambda: None)

        for call in (
            lambda: workplace_service.write_system_prompt_file({"AGENTS.md": "x"}),
            lambda: workplace_service.update_system_prompt_file({"AGENTS.md": "x"}),
        ):
            with pytest.raises(ValueError, match="Invalid file name: AGENTS.md"):
                call()

        # The protected file stays untouched (and is not even exposed by reads).
        assert not (tmp_path / "AGENTS.md").exists()
        assert "AGENTS.md" not in workplace_service.read_system_prompt_file()

    def test_workplace_error_wording(self, tmp_path, monkeypatch):
        from server.service import workplace as workplace_service

        with pytest.raises(ValueError, match="Invalid file name: BAD.md"):
            workplace_service.write_system_prompt_file({"BAD.md": "x"})

    def test_heartbeat_read_write_roundtrip(self, tmp_path, monkeypatch):
        from server.service import heartbeat as heartbeat_service

        hb = tmp_path / "HEARTBEAT.md"
        monkeypatch.setattr(heartbeat_service, "HEARTBEAT_PATH", hb)

        assert heartbeat_service.read_heartbeat_file() == {}
        heartbeat_service.write_heartbeat_file({"HEARTBEAT.md": "# Heartbeat Tasks\n- do x\n"})
        assert heartbeat_service.read_heartbeat_file() == {
            "HEARTBEAT.md": "# Heartbeat Tasks\n- do x\n"
        }

    def test_heartbeat_length_budget_counts_task_text_only(self, tmp_path, monkeypatch):
        from server.service import heartbeat as heartbeat_service

        hb = tmp_path / "HEARTBEAT.md"
        monkeypatch.setattr(heartbeat_service, "HEARTBEAT_PATH", hb)

        # raw length > 2000 but task text ≤ 2000: structural lines don't count
        heading = "# Heartbeat Tasks\n## Active Tasks\n## Completed\n"
        long_structural = heading + ("\n" * 2500) + "- short task\n"
        heartbeat_service.write_heartbeat_file({"HEARTBEAT.md": long_structural})

        # raw length ≤ 2000 but task text > 2000: budget trips
        with pytest.raises(ValueError, match="Content too long for heartbeat file"):
            heartbeat_service.write_heartbeat_file({"HEARTBEAT.md": "- " + ("y" * 2100)})
