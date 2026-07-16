"""Tests for xp_graph.core.extract() �?draft �?ExtractionResult conversion via LLM."""

import pytest
from unittest.mock import patch, MagicMock

from context_engine.xp_graph.core import (
    ExperienceTrace, PathStep, Failure, Fix,
    extract, _serialize_draft,
)
from context_engine.xp_graph.type import NodeType, EdgeType, Node, Edge, ExtractionResult


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def realistic_draft() -> ExperienceTrace:
    """A realistic ExperienceTrace for extract testing."""
    return ExperienceTrace(
        task="Deploy a Python web app to Kubernetes",
        path=[
            PathStep(
                tool="docker_build",
                input="docker build -t myapp .",
                output="Successfully built",
                trigger="Containerize the app for Kubernetes deployment",
            ),
            PathStep(
                tool="kubectl_apply",
                input="kubectl apply -f deployment.yaml",
                output="deployment.apps/myapp created",
                trigger="Kubernetes CLI is the standard way to deploy resources",
            ),
        ],
        failures=[
            Failure(
                symptom="ImportError: libGL.so.1 not found",
                cause="OpenCV dependency libGL1 is missing in Docker image",
                fixes=[
                    Fix(
                        strategy="parameter",
                        description="Install libgl1 in Dockerfile",
                        tool="apt_install",
                    ),
                ],
            ),
        ],
        requires=["python3", "docker"],
    )


@pytest.fixture
def mock_state_register(realistic_draft):
    """Patch state_register_mem to return the realistic draft."""
    with patch("agent.tools.xp_graph.core.state_register_mem") as mock:
        mock.get_state.return_value = realistic_draft
        yield mock


# ─── Tests ───────────────────────────────────────────────────────────


class TestSerializeDraft:
    """Tests for the _serialize_draft helper."""

    def test_serialize_full(self, realistic_draft):
        """Full draft produces expected sections."""
        text = _serialize_draft(realistic_draft)
        assert "Task:" in text
        assert "Execution Path:" in text
        assert "Failures:" in text
        assert "Prerequisites:" in text
        assert "docker_build" in text
        assert "kubectl_apply" in text
        assert "ImportError" in text
        assert "python3" in text

    def test_serialize_empty_path(self):
        """Draft with no path/failures/requires serializes without those sections."""
        draft = ExperienceTrace(task="simple task", path=[], failures=[], requires=None)
        text = _serialize_draft(draft)
        assert "Task: simple task" in text
        assert "Execution Path" not in text
        assert "Failures" not in text
        assert "Prerequisites" not in text


class TestExtractGraph:
    """Tests for extract()."""

    @pytest.fixture(autouse=True)
    def mock_llm(self):
        """Mock main_llm.with_structured_output to return a controlled result."""
        default_result = ExtractionResult(
            nodes=[
                Node(
                    type=NodeType.TASK,
                    name="deploy-python-web-app-to-k8s",
                    description="Deploy a Python web app to Kubernetes",
                    content="deploy-python-web-app-to-k8s\nObjective: Deploy a Python web app to Kubernetes\nSteps:\n1. Build Docker image\n2. Apply Kubernetes manifests\nResult: App deployed successfully",
                ),
                Node(
                    type=NodeType.SKILL,
                    name="docker-build",
                    description="Build Docker images for deployment",
                    content="docker-build\nTrigger: Need to containerize application\nSteps:\n1. docker build -t myapp .\nCommon Errors:\n- Build context too large -> use .dockerignore",
                ),
                Node(
                    type=NodeType.SKILL,
                    name="kubectl-apply",
                    description="Deploy Kubernetes resources via kubectl",
                    content="kubectl-apply\nTrigger: Need to deploy to K8s cluster\nSteps:\n1. kubectl apply -f deployment.yaml\nCommon Errors:\n- YAML syntax error -> validate with kubectl dry-run",
                ),
                Node(
                    type=NodeType.EVENT,
                    name="importerror-libgl1",
                    description="ImportError: libGL.so.1 not found when using OpenCV",
                    content="importerror-libgl1\nSymptom: ImportError: libGL.so.1 not found\nCause: OpenCV dependency libGL1 is missing\nSolution: Install libgl1 in Dockerfile",
                ),
                Node(
                    type=NodeType.SKILL,
                    name="apt-install-libgl",
                    description="Install libgl1 to fix OpenCV dependency",
                    content="apt-install-libgl\nTrigger: ImportError: libGL.so.1\nSteps:\n1. apt install -y libgl1-mesa-glx\nCommon Errors:\n- Permission denied -> use sudo",
                ),
                Node(
                    type=NodeType.SKILL,
                    name="python3",
                    description="Python 3 runtime environment",
                    content="python3\nTrigger: Required as prerequisite for deployment\nSteps:\n1. Ensure python3 is installed",
                ),
                Node(
                    type=NodeType.SKILL,
                    name="docker",
                    description="Docker container runtime",
                    content="docker\nTrigger: Required as prerequisite for deployment\nSteps:\n1. Ensure docker is installed and running",
                ),
            ],
            edges=[
                Edge(from_id="deploy-python-web-app-to-k8s", to_id="docker-build", type=EdgeType.USED_SKILL, instruction="Build Docker image", condition="Containerize the app"),
                Edge(from_id="deploy-python-web-app-to-k8s", to_id="kubectl-apply", type=EdgeType.USED_SKILL, instruction="Deploy with kubectl", condition="Kubernetes CLI is standard"),
                Edge(from_id="importerror-libgl1", to_id="apt-install-libgl", type=EdgeType.SOLVED_BY, instruction="apt install -y libgl1-mesa-glx", condition="ImportError: libGL.so.1 not found"),
                Edge(from_id="deploy-python-web-app-to-k8s", to_id="python3", type=EdgeType.REQUIRES, instruction="Python 3 is required for the app", condition=None),
                Edge(from_id="deploy-python-web-app-to-k8s", to_id="docker", type=EdgeType.REQUIRES, instruction="Docker is required for containerization", condition=None),
            ],
        )
        with patch("models.LLMs.main_llm.main_llm") as mock:
            # RunnableSequence calls mock() directly (not invoke) when mock is not Runnable
            mock_structured = MagicMock(return_value=default_result)
            mock.with_structured_output.return_value = mock_structured
            yield mock_structured

    def test_extract_basic(self, mock_state_register):
        """Happy path: draft produces correct ExtractionResult structure."""
        result = extract(session_id="test-session", role="default")

        # ── Node count ──────────────────────────────────────────
        # TASK(1) + SKILL from path(2) + EVENT(1) + SKILL from fix(1) + SKILL from requires(2) = 7
        assert len(result.nodes) == 7

        # ── Edge count ──────────────────────────────────────────
        # USED_SKILL(2) + SOLVED_BY(1) + REQUIRES(2) = 5
        assert len(result.edges) == 5

        # ── Verify TASK node ────────────────────────────────────
        task_node = next(n for n in result.nodes if n.type == NodeType.TASK)
        assert task_node.name  # non-empty
        assert task_node.description
        assert "Objective:" in task_node.content
        assert "Steps:" in task_node.content
        assert "Result:" in task_node.content

        # ── Verify SKILL nodes exist ────────────────────────────
        skill_nodes = [n for n in result.nodes if n.type == NodeType.SKILL]
        assert len(skill_nodes) >= 4  # path(2) + fix(1) + requires(2 minimum)
        for sn in skill_nodes:
            assert "Trigger:" in sn.content

        # ── Verify all edge types present ───────────────────────
        edge_types = {e.type for e in result.edges}
        assert EdgeType.USED_SKILL in edge_types
        assert EdgeType.SOLVED_BY in edge_types
        assert EdgeType.REQUIRES in edge_types

        # ── Verify directions ──────────────────────────────────
        for e in result.edges:
            assert e.from_id
            assert e.to_id
            assert e.instruction

        used_edges = [e for e in result.edges if e.type == EdgeType.USED_SKILL]
        for e in used_edges:
            assert e.from_id == task_node.name

        req_edges = [e for e in result.edges if e.type == EdgeType.REQUIRES]
        for e in req_edges:
            assert e.from_id == task_node.name

        solved_edges = [e for e in result.edges if e.type == EdgeType.SOLVED_BY]
        for e in solved_edges:
            # from_node should be an EVENT
            evt = next(n for n in result.nodes if n.name == e.from_id)
            assert evt.type == NodeType.EVENT

        # ── Verify EVENT node template ──────────────────────────
        event_node = next(n for n in result.nodes if n.type == NodeType.EVENT)
        assert "Symptom:" in event_node.content
        assert "Cause:" in event_node.content
        assert "Solution:" in event_node.content

    def test_extract_no_state(self):
        """extract raises RuntimeError when no draft exists."""
        with patch("agent.tools.xp_graph.core.state_register_mem") as mock:
            mock.get_state.return_value = None
            with pytest.raises(RuntimeError, match="xp draft is None"):
                extract(session_id="no-draft", role="default")

    def test_extract_empty_path_and_failures(self, mock_state_register, mock_llm):
        """Minimal draft with empty path/failures/null requires �?only TASK node."""
        from context_engine.xp_graph.core import state_register_mem as mock
        mock.get_state.return_value = ExperienceTrace(
            task="simple task",
            path=[],
            failures=[],
            requires=None,
        )
        # RunnableSequence calls mock_structured() not .invoke()
        mock_llm.return_value = ExtractionResult(
            nodes=[
                Node(
                    type=NodeType.TASK,
                    name="simple-task",
                    description="A simple task",
                    content="simple-task\nObjective: simple task\nSteps:\nResult: completed",
                ),
            ],
            edges=[],
        )

        result = extract(session_id="minimal", role="default")

        assert len(result.nodes) == 1
        assert result.nodes[0].type == NodeType.TASK
        assert result.nodes[0].name
        assert len(result.edges) == 0

    def test_extract_reuses_seen_skills(self, mock_state_register, mock_llm):
        """Same tool used repeatedly �?one SKILL node, multiple USED_SKILL edges."""
        from context_engine.xp_graph.core import state_register_mem as mock
        mock.get_state.return_value = ExperienceTrace(
            task="test dedup",
            path=[
                PathStep(tool="web_search", input="query1", output="r1"),
                PathStep(tool="web_search", input="query2", output="r2"),
            ],
            failures=[],
            requires=None,
        )
        mock_llm.return_value = ExtractionResult(
            nodes=[
                Node(type=NodeType.TASK, name="test-dedup", description="Test dedup", content="test-dedup\nObjective: test dedup\nSteps:\n1. web_search\nResult: done"),
                Node(type=NodeType.SKILL, name="web-search", description="Web search skill", content="web-search\nTrigger: Need info\nSteps:\n1. web_search query1\n2. web_search query2"),
            ],
            edges=[
                Edge(from_id="test-dedup", to_id="web-search", type=EdgeType.USED_SKILL, instruction="query1", condition=None),
                Edge(from_id="test-dedup", to_id="web-search", type=EdgeType.USED_SKILL, instruction="query2", condition=None),
            ],
        )

        result = extract(session_id="dedup", role="default")

        # TASK(1) + SKILL(1, not 2)
        assert len(result.nodes) == 2
        skill_nodes = [n for n in result.nodes if n.type == NodeType.SKILL]
        assert len(skill_nodes) == 1

        # Two edges (two USED_SKILL for same skill)
        assert len(result.edges) == 2
        for e in result.edges:
            assert e.to_id == "web-search"

    def test_extract_edge_condition(self, mock_state_register, mock_llm):
        """USED_SKILL edges carry condition."""
        from context_engine.xp_graph.core import state_register_mem as mock
        mock.get_state.return_value = ExperienceTrace(
            task="test condition propagation",
            path=[
                PathStep(
                    tool="git_clone",
                    input="git clone repo",
                    output="done",
                    trigger="Need to get the source code first",
                ),
            ],
            failures=[],
            requires=None,
        )
        mock_llm.return_value = ExtractionResult(
            nodes=[
                Node(type=NodeType.TASK, name="test-condition-propagation", description="Test condition", content="test-condition-propagation\nObjective: test\nSteps:\n1. git_clone\nResult: done"),
                Node(type=NodeType.SKILL, name="git-clone", description="Git clone skill", content="git-clone\nTrigger: Need source code\nSteps:\n1. git clone repo"),
            ],
            edges=[
                Edge(from_id="test-condition-propagation", to_id="git-clone", type=EdgeType.USED_SKILL, instruction="git clone repo", condition="Need to get the source code first"),
            ],
        )

        result = extract(session_id="condition", role="default")

        assert len(result.edges) == 1
        assert result.edges[0].condition == "Need to get the source code first"
        assert result.edges[0].type == EdgeType.USED_SKILL


# ─── Tests for _persist_extraction() (persist branch of extract) ──

class MockGmNode:
    """Minimal GmNode stand-in for mocking find_by_name / upsert_node returns."""
    def __init__(self, id: str, name: str):
        self.id = id
        self.name = name


class TestPersistExtraction:
    """Tests for extract() with db �?persist with dedup + embedding."""

    # ── Fixture: all collaborators mocked ──
    @pytest.fixture(autouse=True)
    def mock_all(self):
        """Mock state_register_mem and every DB-level call extract touches.

        The persist branch lives in TODOManager.py, so patch targets are all in
        agent.tools.xp_graph.core.

        NOTE on patching strategy:
        - extact_graph() does `from models.LLMs.main_llm import main_llm` inside the
          function body (local import), so we must patch at the original module path:
          `models.LLMs.main_llm.main_llm`.
        - This matches what TestExtractGraph.mock_llm does (see above).
        - sync_node_embed and embed_model are module-level imports in TODOManager.py, so we
          patch them at agent.tools.xp_graph.core.
        """
        with (
            patch("agent.tools.xp_graph.core.state_register_mem") as mock_state,
            patch("agent.tools.xp_graph.core.find_by_name") as mock_find,
            patch("agent.tools.xp_graph.core.upsert_node") as mock_upsert_node,
            patch("agent.tools.xp_graph.core.upsert_edge") as mock_upsert_edge,
            patch("agent.tools.xp_graph.core.async_task_queue") as mock_queue,
            patch("agent.tools.xp_graph.core.sync_node_embed") as mock_sync_embed,
            patch("agent.tools.xp_graph.core.embed_model") as mock_embed_model,
            patch("agent.tools.xp_graph.graph.invalidate_graph_cache", new_callable=MagicMock) as mock_invalidate,
            patch("models.LLMs.main_llm.main_llm") as mock_llm,
            patch("agent.tools.xp_graph.store.db.get_db") as mock_get_db,
        ):
            mock_state.get_state.return_value = ExperienceTrace(
                task="Test task",
                path=[PathStep(tool="test_tool", input="input", output="output")],
                failures=[],
                requires=None,
            )
            # The structured LLM chain: ChatPromptTemplate.from_messages([...]) | main_llm.with_structured_output(...)
            # The | operator wraps the MagicMock in a RunnableLambda, which calls __call__ (not .invoke).
            # So we set return_value on the mock itself, not on .invoke.return_value.
            self.mock_structured_llm = MagicMock(name="structured_llm")
            mock_llm.with_structured_output.return_value = self.mock_structured_llm

            self.mock_find = mock_find
            self.mock_upsert_node = mock_upsert_node
            self.mock_upsert_edge = mock_upsert_edge
            self.mock_queue = mock_queue
            self.mock_invalidate = mock_invalidate
            self.mock_sync_embed = mock_sync_embed
            self.mock_embed_model = mock_embed_model

            self.mock_db = MagicMock()
            mock_get_db.return_value = self.mock_db
            yield

    # ── Happy path ──────────────────────────────────────────────
    def test_store_new_nodes_and_edges(self):
        """New nodes �?upsert_node called, sync_node_embed queued, edges upserted."""
        stub_result = ExtractionResult(
            nodes=[
                Node(type=NodeType.TASK, name="test-task", description="A test task", content="test task content"),
                Node(type=NodeType.SKILL, name="test-skill", description="A test skill", content="test skill content"),
            ],
            edges=[
                Edge(from_id="test-task", to_id="test-skill", type=EdgeType.USED_SKILL, instruction="use it", condition=None),
            ],
        )
        node_1 = MockGmNode(id="n-task-1", name="test-task")
        node_2 = MockGmNode(id="n-skill-1", name="test-skill")

        # find_by_name: dedup (2x None) + edge resolution (2x resolved)
        self.mock_find.side_effect = [
            None,                             # dedup: test-task not found
            None,                             # dedup: test-skill not found
            node_1,                           # edge: test-task �?resolved DB node
            node_2,                           # edge: test-skill �?resolved DB node
        ]
        self.mock_upsert_node.side_effect = [
            {"node": node_1, "isNew": True},
            {"node": node_2, "isNew": True},
        ]
        self.mock_structured_llm.return_value = stub_result

        result = extract(
            session_id="s1", role="default",
        )

        # ── Verify upsert_node calls ──
        assert self.mock_upsert_node.call_count == 2
        self.mock_upsert_node.assert_any_call(
            db=self.mock_db,
            c={"type": "TASK", "name": "test-task", "description": "A test task", "content": "test task content"},
            session_id="s1",
        )
        self.mock_upsert_node.assert_any_call(
            db=self.mock_db,
            c={"type": "SKILL", "name": "test-skill", "description": "A test skill", "content": "test skill content"},
            session_id="s1",
        )

        # ── Verify sync_node_embed queued for both new nodes ──
        assert self.mock_queue.add_task.call_count == 2

        # ── Verify edge upsert ──
        self.mock_find.assert_any_call(self.mock_db, "test-task")
        self.mock_find.assert_any_call(self.mock_db, "test-skill")
        self.mock_upsert_edge.assert_called_once_with(
            db=self.mock_db,
            edge_data={
                "from_id": "n-task-1",
                "to_id": "n-skill-1",
                "type": "USED_SKILL",
                "instruction": "use it",
                "condition": None,
                "session_id": "s1",
            },
        )

        # ── Verify cache invalidation ──
        self.mock_invalidate.assert_called_once()

        # ── Verify return value ──
        assert result is stub_result

    def test_store_skips_existing_node(self):
        """Existing node (find_by_name returns not-None) �?skip upsert, no embed.

        Cache is invalidated because name_to_id is populated from existing.id.
        """
        existing_node = MockGmNode(id="n-existing-1", name="test-task")

        stub_result = ExtractionResult(
            nodes=[
                Node(type=NodeType.TASK, name="test-task", description="A test task", content="test task content"),
            ],
            edges=[],
        )
        self.mock_find.return_value = existing_node
        self.mock_structured_llm.return_value = stub_result

        result = extract(
            session_id="s1", role="default",
        )

        # ── No upsert_node called ──
        self.mock_upsert_node.assert_not_called()

        # ── No embed queued ──
        self.mock_queue.add_task.assert_not_called()

        # ── Cache IS invalidated because name_to_id was populated (existing.id) ──
        self.mock_invalidate.assert_called_once()

        # ── Return value intact ──
        assert len(result.nodes) == 1

    def test_store_skip_existing_but_upsert_new(self):
        """Mix of existing & new nodes: only new ones get upserted + embedded."""
        existing_node = MockGmNode(id="n-existing-1", name="existing-node")
        new_node = MockGmNode(id="n-new-1", name="new-node")

        stub_result = ExtractionResult(
            nodes=[
                Node(type=NodeType.TASK, name="existing-node", description="An existing task", content="old content"),
                Node(type=NodeType.SKILL, name="new-node", description="A new skill", content="new content"),
            ],
            edges=[],
        )
        self.mock_find.side_effect = [existing_node, None]
        self.mock_upsert_node.return_value = {"node": new_node, "isNew": True}
        self.mock_structured_llm.return_value = stub_result

        result = extract(
            session_id="s1", role="default",
        )

        # ── Exactly one upsert ──
        self.mock_upsert_node.assert_called_once()
        assert self.mock_upsert_node.call_args[1]["c"]["name"] == "new-node"

        # ── Exactly one embed queued ──
        self.mock_queue.add_task.assert_called_once()

        # ── Cache invalidation triggered (name_to_id non-empty) ──
        self.mock_invalidate.assert_called_once()

    def test_store_edges_with_missing_node_skips_and_warns(self):
        """Edge referencing a node not in DB �?warning logged, edge skipped."""
        skip_node = MockGmNode(id="n-skip-1", name="no-such-node")

        stub_result = ExtractionResult(
            nodes=[
                Node(type=NodeType.TASK, name="my-task", description="A task", content="content"),
            ],
            edges=[
                Edge(from_id="my-task", to_id="no-such-node", type=EdgeType.USED_SKILL, instruction="doesn't matter", condition=None),
            ],
        )
        self.mock_find.side_effect = [
            skip_node,   # dedup: my-task found (existing)
            skip_node,   # edge from: my-task �?resolved DB node
            None,        # edge to: no-such-node not found �?skip
        ]
        self.mock_upsert_node.return_value = {"node": skip_node, "isNew": False}
        self.mock_structured_llm.return_value = stub_result

        with patch("loguru.logger") as mock_logger:
            result = extract(
                session_id="s1", role="default",
            )

        # ── Edge skipped ──
        self.mock_upsert_edge.assert_not_called()

        # ── Warning logged ──
        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "skipping edge" in warning_msg
        assert "no-such-node" in warning_msg

        # ── Cache invalidated (name_to_id was populated from dedup) ──
        self.mock_invalidate.assert_called_once()

    def test_store_empty_extraction(self):
        """Empty ExtractionResult (no nodes) �?nothing happens."""
        stub_result = ExtractionResult(nodes=[], edges=[])
        self.mock_structured_llm.return_value = stub_result

        result = extract(
            session_id="s1", role="default",
        )

        self.mock_find.assert_not_called()
        self.mock_upsert_node.assert_not_called()
        self.mock_queue.add_task.assert_not_called()
        self.mock_upsert_edge.assert_not_called()
        self.mock_invalidate.assert_not_called()

        assert result.nodes == []
        assert result.edges == []

    def test_store_node_type_enum_serialized(self):
        """Node.type as Enum (not str) �?.value extracted correctly."""
        stub_result = ExtractionResult(
            nodes=[
                Node(type=NodeType.TASK, name="enum-task", description="Enum task", content="content"),
            ],
            edges=[],
        )
        self.mock_find.return_value = None
        self.mock_upsert_node.return_value = {"node": MockGmNode(id="n-enum-1", name="enum-task"), "isNew": True}
        self.mock_structured_llm.return_value = stub_result

        result = extract(
            session_id="s1", role="default",
        )

        call_args = self.mock_upsert_node.call_args[1]["c"]
        assert call_args["type"] == "TASK"

    def test_store_edge_type_enum_serialized(self):
        """Edge.type as Enum �?.value extracted for upsert_edge call."""
        stub_result = ExtractionResult(
            nodes=[
                Node(type=NodeType.TASK, name="t1", description="d1", content="c1"),
                Node(type=NodeType.SKILL, name="s1", description="d2", content="c2"),
            ],
            edges=[
                Edge(from_id="t1", to_id="s1", type=EdgeType.USED_SKILL, instruction="do it", condition=None),
            ],
        )
        self.mock_find.side_effect = [
            None, None,
            MockGmNode(id="n-t1", name="t1"),
            MockGmNode(id="n-s1", name="s1"),
        ]
        self.mock_upsert_node.side_effect = [
            {"node": MockGmNode(id="n-t1", name="t1"), "isNew": True},
            {"node": MockGmNode(id="n-s1", name="s1"), "isNew": True},
        ]
        self.mock_structured_llm.return_value = stub_result

        result = extract(
            session_id="s1", role="default",
        )

        edge_data = self.mock_upsert_edge.call_args[1]["edge_data"]
        assert edge_data["type"] == "USED_SKILL"
