"""Hermetic tests for the Docker-free DeepSWE local runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals.deepswe import instances, local

pytestmark = [pytest.mark.unit]


def _task(
    task_dir: Path,
    repo: str = "https://example.test/repo",
    base_commit: str = "abc123",
) -> instances.Task:
    return instances.Task(
        task_id="demo-task",
        task_dir=task_dir,
        repo=repo,
        base_commit=base_commit,
        language="python",
        image="example/image:tag",
        verifier_timeout_s=60.0,
        verifier_network="no-network",
        build_timeout_s=120.0,
        instruction="Fix the demo bug.",
    )


def _make_task_dir(root: Path) -> Path:
    task_dir = root / "demo-task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "environment").mkdir()
    (task_dir / "solution").mkdir()
    (task_dir / "instruction.md").write_text("Fix the demo bug.", encoding="utf-8")
    (task_dir / "tests" / "config.json").write_text(
        json.dumps(
            {
                "base_commit": "abc123",
                "grade": {
                    "format": "junit",
                    "reports": ["/logs/verifier/base.xml", "/logs/verifier/new.xml"],
                },
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "tests" / "test.sh").write_text(
        "#!/bin/bash\ncd /app\npython3 /tests/grader.py prepare\n"
        "PYTEST_ADDOPTS='--junitxml=/logs/verifier/base.xml' bash /app/test.sh base\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "grader.py").write_text("# stub\n", encoding="utf-8")
    (task_dir / "tests" / "test.patch").write_text("", encoding="utf-8")
    (task_dir / "solution" / "solution.patch").write_text("", encoding="utf-8")
    return task_dir


def test_pip_commands_parse_multiline_and_python_m(tmp_path: Path):
    task_dir = _make_task_dir(tmp_path)
    (task_dir / "environment" / "Dockerfile").write_text(
        "FROM python:3.12\n"
        "RUN python -m pip install -r requirements.txt\n"
        "RUN cd /app && pip install -e . && \\\n"
        "    pip install pytest pytest-timeout\n",
        encoding="utf-8",
    )

    commands = local._pip_commands(_task(task_dir))

    assert ["-r", "requirements.txt"] in commands
    assert ["-e", "."] in commands
    assert ["pytest", "pytest-timeout"] in commands


def test_pip_commands_fallbacks_to_editable_install(tmp_path: Path):
    task_dir = _make_task_dir(tmp_path)

    assert local._pip_commands(_task(task_dir)) == [["-e", "."]]


def test_copy_local_tests_rewrites_report_paths(tmp_path: Path):
    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "verifier"

    tests_dir = local._copy_local_tests(_task(task_dir), out_dir)

    config = json.loads((tests_dir / "config.json").read_text(encoding="utf-8"))
    assert config["grade"]["reports"] == [
        str(out_dir / "base.xml"),
        str(out_dir / "new.xml"),
    ]
    assert (tests_dir / "test.sh").read_text(encoding="utf-8").startswith("#!/bin/bash")


def test_localize_verifier_script_rewrites_container_paths(tmp_path: Path):
    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "verifier"
    repo = tmp_path / "repo"
    tests_dir = local._copy_local_tests(_task(task_dir), out_dir)

    script = local._localize_verifier_script(_task(task_dir), repo, out_dir, tests_dir)
    text = script.read_text(encoding="utf-8")

    assert "cd /app" not in text
    assert "/logs/verifier" not in text
    assert f"python3 {tests_dir}/grader.py" in text
    assert str(repo) in text
    assert str(tests_dir) in text
    assert str(out_dir) in text


def test_test_paths_marks_held_out_files_only():
    paths = ["src/pkg.py", "tests/test_pkg.py", "conftest.py", "docs/guide.md"]

    assert local._test_paths(paths) == ["tests/test_pkg.py", "conftest.py"]


def test_python_version_uses_requires_python_floor(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.13"\n', encoding="utf-8"
    )

    assert local._python_version(repo) == "3.13"

    (repo / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.9"\n', encoding="utf-8")
    assert local._python_version(repo) == "3.12"


def test_local_env_strips_proxy_and_sets_task_venv(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    out_dir = tmp_path / "verifier"
    tests_dir = tmp_path / "tests"
    artifacts = out_dir / "artifacts"
    for path in (repo, out_dir, tests_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:1")
    monkeypatch.setenv("PIP_INDEX_URL", "https://pip.invalid/simple")

    env = local._local_env(repo, out_dir, tests_dir, artifacts)

    assert "HTTP_PROXY" not in env
    assert "https_proxy" not in env
    assert "PIP_INDEX_URL" not in env
    assert env["VIRTUAL_ENV"] == str(repo / ".venv")
    assert env["TESTS_DIR"] == str(tests_dir)
    assert env["PATH"].startswith(str(repo / ".venv" / "bin"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_clone_repo_time_travels_and_prunes_future(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "eval@example.test")
    _git(source, "config", "user.name", "Eval")
    (source / "state.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "base")
    base = _git(source, "rev-parse", "HEAD")
    (source / "state.txt").write_text("future\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "future")
    _git(source, "tag", "future-tag")

    dest = tmp_path / "clone"
    local._clone_repo(_task(tmp_path, repo=str(source), base_commit=base), dest)

    assert (dest / "state.txt").read_text(encoding="utf-8") == "base\n"
    assert _git(dest, "rev-parse", "HEAD") == base
    assert "future-tag" not in _git(dest, "tag").split()
    assert _git(dest, "for-each-ref", "--format=%(refname:short)", "refs/heads") == "local-eval"
    assert _git(dest, "remote") == ""


def test_agent_patch_excludes_harness_only_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "eval@example.test")
    _git(repo, "config", "user.name", "Eval")
    (repo / "src").mkdir()
    (repo / "src" / "pkg.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    local._write_excludes(repo)
    (repo / "src" / "pkg.py").write_text("value = 2\n", encoding="utf-8")
    (repo / ".sherry").mkdir()
    (repo / ".sherry" / "state.json").write_text("{}\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("venv\n", encoding="utf-8")

    patch = local._agent_patch(repo, base)

    assert "src/pkg.py" in patch
    assert ".sherry/" not in patch
    assert ".venv/" not in patch
