"""WorkspaceNoticeMiddleware: one notice per change, at send time.

The middleware is purely a comparison + splice: it reads the effective root and
that directory's git branch/HEAD, compares each against what the agent was last
told about, and either does nothing or returns a state update whose message list
carries ONE notice per changed kind immediately before the turn's HumanMessage.
These tests drive the hook directly (no graph needed) and pin the contract: the
baseline is silent, every real change is announced exactly once, several switches
coalesce into the final state, a switch back announces nothing, the injected
messages never displace the human message from the end, and — the reason this
file doubles as the HumanMessage regression suite — every notice is a
``HumanMessage`` (never an ``AIMessage``) tagged with the shared workspace-notice
metadata, which is what keeps the chat rendering it as a neutral system card.
"""

import asyncio
import subprocess

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from agent.middlewares.workspace_notice import WorkspaceNoticeMiddleware
from agent.middlewares.workspace_notice import core as notice_core
from pub.func.message.workspace_notice import is_workspace_notice
from runtime.session.git_head import (
    announced_git_head,
    prime_git_head_from_store,
    read_git_head,
    record_announced_git_head,
)
from runtime.session.project_dir import (
    announced_project_dir,
    current_project_dir,
    prime_mem_from_store,
    record_announced_project_dir,
    write_project_dir,
)
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.unit]

SESSION = "sess-project-dir-notice"
_ROOT_A = "/tmp/sherry-notice-a"
_ROOT_B = "/tmp/sherry-notice-b"
_ROOT_C = "/tmp/sherry-notice-c"


class _InMemoryRegisterDB:
    """Stand-in for ``state_register_db`` (never touch the real SQLite file)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, object]] = {}

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self._states.setdefault(session_id, {})[str(key)] = value
        return True

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self._states.get(session_id, {}).get(str(key), default)

    def delete_state(self, session_id: str, key: str) -> bool:
        return self._states.get(session_id, {}).pop(str(key), None) is not None

    def get_all_session_ids(self) -> list[str]:
        return list(self._states)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch):
    """Swap the durable register for a fake and clear the mem tier per test."""
    import runtime

    monkeypatch.setattr(runtime, "state_register_db", _InMemoryRegisterDB())
    state_register_mem.clear_session(SESSION)
    yield
    state_register_mem.clear_session(SESSION)


def _bind(root: str) -> None:
    state_register_mem.set_state(SESSION, StateKey.PROJECT_DIR, root)


def _state(messages: list) -> dict:
    return {"session_id": SESSION, "messages": messages}


def _run(middleware: WorkspaceNoticeMiddleware, messages: list) -> dict | None:
    return asyncio.run(middleware.abefore_agent(_state(messages)))


def _body(update: dict) -> list:
    """The message list of a notice update, with the leading RemoveMessage checked."""
    messages = update["messages"]
    assert isinstance(messages[0], RemoveMessage)
    assert messages[0].id == REMOVE_ALL_MESSAGES, "the rewrite must clear the channel first"
    return messages[1:]


def _git(root, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)


@pytest.fixture
def git_repo(tmp_path):
    """A real repository on ``main`` with one commit (the git notice's subject)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "note.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_the_first_message_records_the_baseline_silently():
    """Nothing to announce: the agent is not told about a change that never happened."""
    _bind(_ROOT_A)
    middleware = WorkspaceNoticeMiddleware()

    assert _run(middleware, [HumanMessage("你好")]) is None
    assert announced_project_dir(SESSION) == _ROOT_A


def test_a_changed_root_is_announced_once_right_before_the_human_message():
    _bind(_ROOT_A)
    record_announced_project_dir(SESSION, _ROOT_A)
    _bind(_ROOT_B)
    earlier, answer, request = (
        HumanMessage("先前的请求"),
        AIMessage("好的"),
        HumanMessage("现在换目录了"),
    )
    middleware = WorkspaceNoticeMiddleware()

    update = _run(middleware, [earlier, answer, request])

    assert update is not None
    body = _body(update)
    # [earlier, answer, NOTICE, request] — the human message keeps its last place.
    assert body[:2] == [earlier, answer]
    assert body[-1] is request
    notice = body[-2]
    # The notice is a HUMAN message (the injected-carrier role), not an AI answer:
    # the persistence layer renders a human row with a non-``user`` origin as a
    # neutral system card, which is exactly what a workspace notice is.
    assert isinstance(notice, HumanMessage)
    assert not isinstance(notice, AIMessage)
    assert _ROOT_A in notice.content and _ROOT_B in notice.content
    assert notice.metadata["origin"] == "project_dir"
    assert notice.metadata["internal"] is True
    assert notice.metadata["provenance"] == "workspace_notice"
    assert is_workspace_notice(notice) is True
    assert announced_project_dir(SESSION) == _ROOT_B, "the baseline advances with the notice"


def test_the_notice_is_sent_only_once_for_the_change():
    """A second call in the same turn (tool loop) must not stack a second notice."""
    _bind(_ROOT_B)
    record_announced_project_dir(SESSION, _ROOT_A)
    middleware = WorkspaceNoticeMiddleware()
    messages = [HumanMessage("继续")]

    first = _run(middleware, messages)
    second = _run(middleware, list(messages))

    assert first is not None
    assert second is None


def test_several_switches_coalesce_into_the_final_root():
    """A→B→C between two messages yields ONE notice that names A and C, never B."""
    _bind(_ROOT_A)
    record_announced_project_dir(SESSION, _ROOT_A)
    _bind(_ROOT_B)
    _bind(_ROOT_C)
    middleware = WorkspaceNoticeMiddleware()

    update = _run(middleware, [HumanMessage("做点事")])

    assert update is not None
    notice = _body(update)[-2]
    assert _ROOT_A in notice.content and _ROOT_C in notice.content
    assert _ROOT_B not in notice.content, "intermediate roots are not worth a message"
    assert announced_project_dir(SESSION) == _ROOT_C


def test_switching_back_to_the_announced_root_sends_nothing():
    _bind(_ROOT_A)
    record_announced_project_dir(SESSION, _ROOT_A)
    middleware = WorkspaceNoticeMiddleware()

    assert _run(middleware, [HumanMessage("还在同一个目录")]) is None


def test_a_turn_without_a_human_message_appends_the_notice():
    """A resumed / carrier turn has no slot to splice into; the notice still lands."""
    _bind(_ROOT_B)
    record_announced_project_dir(SESSION, _ROOT_A)
    middleware = WorkspaceNoticeMiddleware()
    tail = AIMessage("（被打断的助手消息）")

    update = _run(middleware, [tail])

    assert update is not None
    body = _body(update)
    assert body[-1] is not tail and isinstance(body[-1], HumanMessage)
    assert _ROOT_B in body[-1].content


def test_unbinding_is_announced_with_the_no_project_directory_wording():
    _bind(_ROOT_A)
    record_announced_project_dir(SESSION, _ROOT_A)
    state_register_mem.delete_state(SESSION, StateKey.PROJECT_DIR)
    middleware = WorkspaceNoticeMiddleware()

    update = _run(middleware, [HumanMessage("继续")])

    assert update is not None
    notice = _body(update)[-2]
    assert "解除绑定" in notice.content
    assert "no project directory bound" in notice.content
    assert _ROOT_A in notice.content


def test_a_broken_read_is_fail_open(monkeypatch: pytest.MonkeyPatch):
    middleware = WorkspaceNoticeMiddleware()

    def _boom(_session_id: str | None):
        raise RuntimeError("register down")

    monkeypatch.setattr(notice_core, "current_project_dir", _boom)

    assert _run(middleware, [HumanMessage("继续")]) is None


def test_a_blank_session_id_is_a_no_op():
    middleware = WorkspaceNoticeMiddleware()

    blank = asyncio.run(
        middleware.abefore_agent({"session_id": "  ", "messages": [HumanMessage("x")]})
    )

    assert blank is None


def test_the_sync_hook_matches_the_async_one():
    """Non-async graphs use ``before_agent``; the behaviour must be identical."""
    _bind(_ROOT_B)
    record_announced_project_dir(SESSION, _ROOT_A)
    middleware = WorkspaceNoticeMiddleware()

    update = middleware.before_agent(_state([HumanMessage("继续")]))

    assert update is not None
    assert isinstance(_body(update)[-2], HumanMessage)
    assert _ROOT_B in _body(update)[-2].content


def test_the_baseline_survives_a_restart_through_the_durable_mirror():
    """The primed baseline keeps a change announced that happened before the restart."""
    write_project_dir(SESSION, _ROOT_A)
    record_announced_project_dir(SESSION, _ROOT_A)
    write_project_dir(SESSION, _ROOT_C)

    # Simulate the restart: the mem tier is gone, the durable mirror is not.
    state_register_mem.clear_session(SESSION)
    assert announced_project_dir(SESSION) is None
    assert prime_mem_from_store() == 1
    assert announced_project_dir(SESSION) == _ROOT_A

    update = _run(WorkspaceNoticeMiddleware(), [HumanMessage("重启后继续")])

    assert update is not None
    notice = _body(update)[-2]
    assert _ROOT_A in notice.content and _ROOT_C in notice.content


def test_the_process_default_is_reported_when_a_session_is_unbound():
    """An unbound session's effective root is the process default, not a project."""
    state_register_mem.delete_state(SESSION, StateKey.PROJECT_DIR)
    record_announced_project_dir(SESSION, _ROOT_A)
    middleware = WorkspaceNoticeMiddleware()

    update = _run(middleware, [HumanMessage("继续")])

    assert update is not None
    notice = _body(update)[-2]
    effective = str(current_project_dir(SESSION))
    assert effective in notice.content
    assert effective != _ROOT_A, "the memo must name the new effective root"


class TestGitHeadNotice:
    """The git half: branch + HEAD, compared per turn, announced like the root."""

    def test_the_first_turn_baselines_the_branch_silently(self, git_repo):
        _bind(str(git_repo))
        middleware = WorkspaceNoticeMiddleware()

        assert _run(middleware, [HumanMessage("开始")]) is None
        assert announced_git_head(SESSION) == read_git_head(git_repo)
        assert announced_git_head(SESSION).startswith("main@")

    def test_a_branch_switch_is_announced_before_the_human_message(self, git_repo):
        _bind(str(git_repo))
        middleware = WorkspaceNoticeMiddleware()
        _run(middleware, [HumanMessage("开始")])
        _git(git_repo, "checkout", "-q", "-b", "feature")
        request = HumanMessage("接着做")

        update = _run(middleware, [AIMessage("好的"), request])

        assert update is not None
        body = _body(update)
        assert body[-1] is request
        notice = body[-2]
        assert isinstance(notice, HumanMessage)
        assert notice.metadata["origin"] == "git_head"
        assert notice.metadata["provenance"] == "workspace_notice"
        assert "main@" in notice.content and "feature@" in notice.content
        assert str(git_repo) in notice.content, "the notice names the repository it is about"
        assert announced_git_head(SESSION).startswith("feature@")

    def test_a_new_commit_on_the_same_branch_is_a_change(self, git_repo):
        """Same branch, different HEAD — exactly the "not the same branch head" case."""
        _bind(str(git_repo))
        middleware = WorkspaceNoticeMiddleware()
        _run(middleware, [HumanMessage("开始")])
        before = announced_git_head(SESSION)
        (git_repo / "note.txt").write_text("two\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-q", "-m", "another")

        update = _run(middleware, [HumanMessage("继续")])

        assert update is not None
        notice = _body(update)[-2]
        assert isinstance(notice, HumanMessage)
        assert before in notice.content
        assert announced_git_head(SESSION) != before

    def test_the_same_branch_and_head_sends_nothing(self, git_repo):
        _bind(str(git_repo))
        middleware = WorkspaceNoticeMiddleware()
        _run(middleware, [HumanMessage("开始")])

        assert _run(middleware, [HumanMessage("继续")]) is None

    def test_a_directory_switch_and_a_branch_switch_are_two_notices(self, git_repo, tmp_path):
        """The new root is another repository: both facts changed, both are named."""
        _bind(_ROOT_A)
        record_announced_project_dir(SESSION, _ROOT_A)
        record_announced_git_head(SESSION, "main@deadbee1")
        _bind(str(git_repo))
        middleware = WorkspaceNoticeMiddleware()
        request = HumanMessage("在新项目里继续")

        update = _run(middleware, [request])

        assert update is not None
        body = _body(update)
        assert body[-1] is request
        notices = body[:-1]
        assert [n.metadata["origin"] for n in notices] == ["project_dir", "git_head"]
        assert all(isinstance(n, HumanMessage) for n in notices)
        # The git notice uses the "switched" wording: the old token belongs to
        # another repository, so it must not claim this repository moved from it.
        assert "working directory moved to" in notices[1].content
        assert str(git_repo) in notices[1].content

    def test_a_non_repository_root_never_produces_a_git_notice(self):
        _bind(_ROOT_B)
        middleware = WorkspaceNoticeMiddleware()

        assert _run(middleware, [HumanMessage("这里没有仓库")]) is None
        assert announced_git_head(SESSION) is None, "no token, no baseline to compare"

    def test_a_repository_appearing_later_still_baselines_silently(self, git_repo, tmp_path):
        """A missing token must not be remembered as 'the branch moved'."""
        _bind(_ROOT_B)
        middleware = WorkspaceNoticeMiddleware()
        _run(middleware, [HumanMessage("先在没有仓库的目录")])

        _bind(str(git_repo))
        assert _run(middleware, [HumanMessage("换到仓库")]) is not None  # the ROOT moved
        assert announced_git_head(SESSION) == read_git_head(git_repo)
        assert _run(middleware, [HumanMessage("继续")]) is None  # the branch did not

    def test_the_git_baseline_survives_a_restart_through_the_durable_mirror(self, git_repo):
        _bind(str(git_repo))
        token = read_git_head(git_repo)
        record_announced_git_head(SESSION, token)

        state_register_mem.clear_session(SESSION)
        assert announced_git_head(SESSION) is None
        assert prime_git_head_from_store() == 1
        assert announced_git_head(SESSION) == token
