"""End-to-end media pipeline through the real agent graph.

Covers the five image plans at their seams: the server's outgoing compression,
the artifact store and its HTTP route, ``read_file`` handing an image/PDF to a
vision model, and the request-time history media budget. The model is a
deterministic scripted stub — no network, no real provider.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver

from agent.middlewares import llm_capability_cache
from agent.middlewares.llm_retry import LLMRetryMiddleware
from agent.middlewares.llm_retry.core import LLMRetryConfig
from agent.middlewares.media_pipeline import core as mm_core
from agent.middlewares.media_pipeline import fallback as mm_fallback
from agent.middlewares.summarization import media_offload
from agent.middlewares.summarization.media_offload import offload_inline_media
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.pub_base import artifact_store, path_utils, pdf_render
from config.features import MEDIA_PIPELINE
from pub.types.message import MultiModalMessage
from runtime import state_register_mem
from server.service.messages import _get_content_list

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

PROVIDER = "e2e-media-provider"
MODEL = "e2e-media-model"
SESSIONS = (
    "e2e-media-upload",
    "e2e-media-read",
    "e2e-media-pdf",
    "e2e-media-pdf-blind",
    "e2e-media-pdf-render",
)


def _png(size: int = 64, *, noise: bool = False) -> bytes:
    image = (
        Image.effect_noise((size, size), 100).convert("RGB")
        if noise
        else Image.new("RGB", (size, size), (30, 140, 220))
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _decode_data_url(url: str) -> bytes:
    return base64.b64decode(url.split(",", 1)[1])


class _ScriptedModel(BaseChatModel):
    """Returns the next scripted tool call, else a final text answer."""

    script: ClassVar[list[dict | None]] = []
    calls: ClassVar[list[list]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - Runnable contract
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls.append(list(messages))
        index = len(type(self).calls) - 1
        spec = type(self).script[index] if index < len(type(self).script) else None
        if spec is not None:
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[spec]))]
            )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="final reply"))])


def _build_graph(tools: tuple = ()):
    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentState

    class HarnessState(AgentState):
        session_id: str

    return create_agent(
        model=_ScriptedModel(),
        state_schema=HarnessState,
        checkpointer=MemorySaver(),
        tools=list(tools),
        middleware=[
            mm_core.MultimodalProcessor(),
            LLMRetryMiddleware(config=LLMRetryConfig(max_retries=0)),
        ],
    )


def _invoke(graph, session_id: str, message: HumanMessage) -> list:
    out = graph.invoke(
        {"messages": [message], "session_id": session_id},
        {"configurable": {"thread_id": session_id, "session_id": session_id}},
    )
    return out["messages"]


def _tool_messages(messages: list) -> list[ToolMessage]:
    return [message for message in messages if isinstance(message, ToolMessage)]


def _image_blocks(message) -> list[dict]:
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return []
    return [
        item
        for item in content
        if isinstance(item, dict) and item.get("type") in ("image_url", "document", "file")
    ]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    src_root = tmp_path / "src"
    project_root = (tmp_path / "project").resolve()
    project_root.mkdir()
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(project_root))
    monkeypatch.setattr(path_utils, "ROOT_DIR", project_root)
    monkeypatch.setattr(mm_core, "SRC_DIR", src_root)
    monkeypatch.setattr(mm_fallback, "SRC_DIR", src_root)
    monkeypatch.setattr(artifact_store, "SRC_DIR", src_root)
    monkeypatch.setattr(media_offload, "SESSIONS_DIR", tmp_path / "sessions")
    import server.trigger.http.artifacts as artifacts_http

    monkeypatch.setattr(artifacts_http, "SRC_DIR", src_root)
    monkeypatch.setenv("MAIN_LLM_PROVIDER", PROVIDER)
    monkeypatch.setenv("MAIN_LLM_NAME", MODEL)
    llm_capability_cache.reset_cache()
    _ScriptedModel.script = []
    _ScriptedModel.calls = []
    for session in SESSIONS:
        state_register_mem.clear_session(session)
    yield project_root
    llm_capability_cache.reset_cache()
    for session in SESSIONS:
        state_register_mem.clear_session(session)


def test_upload_is_compressed_persisted_and_served_end_to_end(_isolated_env):
    raw = _png(1400, noise=True)
    content = _get_content_list(
        MultiModalMessage(text="describe this", image_base64_list=[base64.b64encode(raw).decode()])
    )
    url = content[1]["image_url"]["url"]
    sent = _decode_data_url(url)

    assert len(sent) < len(raw), "the server send path must compress the upload"
    with Image.open(io.BytesIO(sent)) as image:
        assert max(image.size) <= 2000

    session_id = "e2e-media-upload"
    graph = _build_graph()
    messages = _invoke(graph, session_id, HumanMessage(content=content))

    first_call = _ScriptedModel.calls[0]
    user_message = next(m for m in first_call if isinstance(m, HumanMessage))
    assert _image_blocks(user_message), "the vision model must receive the image block"

    stored = user_message.additional_kwargs["images"]
    assert stored and stored[0].startswith(f"sherry-artifact://{session_id}/")
    name = stored[0].rsplit("/", 1)[-1]
    artifact_path = Path(mm_core.SRC_DIR) / session_id / "artifacts" / name
    assert artifact_path.is_file()

    import server.trigger.http.artifacts as artifacts_http

    response = asyncio.run(
        artifacts_http.get_artifact(None, {"session_id": session_id, "name": name})
    )
    assert int(response.status_code) == 200
    assert response.description == artifact_path.read_bytes()
    assert messages[-1].content == "final reply"

    # Compression-time offload reuses the stored artifact instead of writing a
    # second copy under SESSIONS_DIR/<sid>/media.
    inline = HumanMessage(content=content)
    pointer = offload_inline_media([inline], session_id)[0].content[1]["text"]
    assert pointer == f"[evicted to: {artifact_path}]"
    assert not (Path(media_offload.SESSIONS_DIR) / session_id / "media").exists()


def test_read_file_image_and_history_budget_end_to_end(_isolated_env, monkeypatch):
    llm_capability_cache.set_capability(PROVIDER, MODEL, "vision", "supported")
    image_path = _isolated_env / "shot.png"
    image_path.write_bytes(_png(96))

    _ScriptedModel.script = [
        {
            "name": "read_file",
            "args": {"file_path": str(image_path)},
            "id": "call-read-image",
        },
        None,
        None,
    ]
    graph = _build_graph(tools=(build_read_file_tool(),))
    session_id = "e2e-media-read"

    _invoke(graph, session_id, HumanMessage(content="read shot.png"))
    tool_message = _tool_messages(_ScriptedModel.calls[1])[-1]
    assert isinstance(tool_message.content, list)
    assert any(block.get("type") == "image_url" for block in tool_message.content)
    assert "[Image read:" in tool_message.content[0]["text"]

    # A later text turn keeps the tool image while it fits the 75 MiB budget.
    _invoke(graph, session_id, HumanMessage(content="what did you see?"))
    second_turn_tool = _tool_messages(_ScriptedModel.calls[2])[-1]
    assert any(block.get("type") == "image_url" for block in second_turn_tool.content)

    # A zero budget drops the non-protected history image on the next request.
    monkeypatch.setitem(MEDIA_PIPELINE, "history_media_budget_bytes", 0)
    _invoke(graph, session_id, HumanMessage(content="and now?"))
    projected_tool = _tool_messages(_ScriptedModel.calls[3])[-1]
    assert isinstance(projected_tool.content, list)
    assert not any(block.get("type") == "image_url" for block in projected_tool.content)
    assert any(
        "media budget" in block.get("text", "")
        for block in projected_tool.content
        if isinstance(block, dict)
    )


def test_read_file_pdf_native_block_end_to_end(_isolated_env):
    llm_capability_cache.set_capability(PROVIDER, MODEL, "pdf", "supported")
    llm_capability_cache.set_capability(PROVIDER, MODEL, "vision", "supported")
    pdf_path = _isolated_env / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nfake\n")

    _ScriptedModel.script = [
        {"name": "read_file", "args": {"file_path": str(pdf_path)}, "id": "call-read-pdf"},
        None,
    ]
    graph = _build_graph(tools=(build_read_file_tool(),))

    _invoke(graph, "e2e-media-pdf", HumanMessage(content="read doc.pdf"))

    tool_message = _tool_messages(_ScriptedModel.calls[1])[-1]
    assert isinstance(tool_message.content, list)
    block = next(item for item in tool_message.content if item.get("type") == "file")
    # LangChain normalizes the provider-native block into its FileContentBlock
    # before the request reaches the model adapter; the bytes must survive.
    assert block["mime_type"] == "application/pdf"
    assert base64.b64decode(block["base64"]) == pdf_path.read_bytes()


def test_read_file_pdf_renders_pages_for_a_vision_only_model(_isolated_env, monkeypatch):
    llm_capability_cache.set_capability(PROVIDER, MODEL, "pdf", "unsupported")
    llm_capability_cache.set_capability(PROVIDER, MODEL, "vision", "supported")
    pdf_path = _isolated_env / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nfake\n")
    page = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 10, 10)).save(page, format="JPEG")

    monkeypatch.setattr(
        pdf_render,
        "render_pdf_pages",
        lambda *args, **kwargs: [pdf_render.PdfPage(1, page.getvalue(), 32, 32)],
    )
    _ScriptedModel.script = [
        {"name": "read_file", "args": {"file_path": str(pdf_path)}, "id": "call-read-pdf-3"},
        None,
    ]
    graph = _build_graph(tools=(build_read_file_tool(),))

    _invoke(graph, "e2e-media-pdf-render", HumanMessage(content="read doc.pdf"))

    tool_message = _tool_messages(_ScriptedModel.calls[1])[-1]
    assert isinstance(tool_message.content, list)
    image_block = next(item for item in tool_message.content if item.get("type") == "image_url")
    assert image_block["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_read_file_pdf_without_a_renderer_reports_unavailable(_isolated_env, monkeypatch):
    llm_capability_cache.set_capability(PROVIDER, MODEL, "pdf", "unsupported")
    llm_capability_cache.set_capability(PROVIDER, MODEL, "vision", "supported")
    pdf_path = _isolated_env / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nfake\n")

    def _unavailable(*args, **kwargs):  # noqa: ANN002, ANN003
        raise pdf_render.PdfRenderUnavailable("no renderer")

    monkeypatch.setattr(pdf_render, "render_pdf_pages", _unavailable)
    _ScriptedModel.script = [
        {"name": "read_file", "args": {"file_path": str(pdf_path)}, "id": "call-read-pdf-2"},
        None,
    ]
    graph = _build_graph(tools=(build_read_file_tool(),))

    _invoke(graph, "e2e-media-pdf-blind", HumanMessage(content="read doc.pdf"))

    tool_message = _tool_messages(_ScriptedModel.calls[1])[-1]
    assert isinstance(tool_message.content, str)
    assert json.loads(tool_message.content)["error"] == "pdf_render_unavailable"
