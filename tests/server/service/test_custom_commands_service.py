"""Custom slash-command discovery for the composer."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.service import custom_commands_service as commands


def _write(path: Path, *, description: str, body: str, hint: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hint_line = f"argument-hint: {hint}\n" if hint else ""
    path.write_text(
        f"---\ndescription: {description}\n{hint_line}---\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def isolated_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    user = tmp_path / "home" / "commands"
    project = tmp_path / "project"
    monkeypatch.setattr(commands, "APP_HOME", tmp_path / "home")
    monkeypatch.setattr(commands, "current_project_dir", lambda _session_id: project)
    return user, project


def test_user_and_workspace_commands_merge_with_workspace_winning(
    isolated_commands: tuple[Path, Path],
) -> None:
    user, project = isolated_commands
    _write(user / "review.md", description="Review a diff", body="Review $ARGUMENTS", hint="[area]")
    _write(user / "nested" / "code.md", description="Nested", body="Nested body")
    _write(
        project / ".sherry" / "commands" / "review.md", description="Workspace review", body="Local"
    )
    _write(project / ".sherry" / "commands" / "deploy.md", description="Deploy", body="Ship it")

    found = {item["name"]: item for item in commands.list_custom_commands("sess")}

    assert found["review"]["source"] == "workspace"
    assert found["review"]["description"] == "Workspace review"
    assert found["review"]["body"] == "Local"
    assert found["nested:code"]["body"] == "Nested body"
    assert found["deploy"]["source"] == "workspace"


def test_malformed_or_descriptionless_files_are_skipped(
    isolated_commands: tuple[Path, Path],
) -> None:
    user, project = isolated_commands
    _write(user / "ok.md", description="Good", body="Body")
    (user / "no-frontmatter.md").write_text("plain text", encoding="utf-8")
    _write(user / "no-description.md", description="", body="Body")
    _write(user / "Bad Name.md", description="Bad", body="Body")

    names = [item["name"] for item in commands.list_custom_commands("sess")]

    assert names == ["ok"]


def test_workspace_root_failure_is_fail_open(
    isolated_commands: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _project = isolated_commands
    _write(user / "ok.md", description="Good", body="Body")
    monkeypatch.setattr(
        commands, "current_project_dir", lambda _session_id: (_ for _ in ()).throw(OSError)
    )

    assert [item["name"] for item in commands.list_custom_commands("sess")] == ["ok"]
