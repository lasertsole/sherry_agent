import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from loguru import logger
from config import SRC_DIR
from config.features import MEDIA_PIPELINE
from typing import override
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from ..llm_capability_cache import get_capability, get_model_key
from .fallback import apply_skill_fallback as apply_skill_fallback
from .fallback import attach_media_hints
from .mixins import BeforeAgentHooksMixin, AfterAgentHooksMixin
from .media_handlers import MediaPaths, MediaType, _MEDIA_HANDLERS
from .scrub import scrub_messages
from pub.func.validator import is_safe_session_id
from runtime import state_register_mem

MULTIMODAL_TRYING_NATIVE_KEY = "_multimodal_trying_native"
MULTIMODAL_NATIVE_MODEL_KEY = "_multimodal_native_model"


@dataclass
class _TurnMedia:
    """The media state of one processed turn: the last message, its text block
    (media hints are appended here) and the paths collected while persisting."""

    state_mes_list: list[BaseMessage]
    last_mes: HumanMessage
    text_dict: dict[str, Any]
    paths: MediaPaths


class MultimodalProcessor(BeforeAgentHooksMixin, AfterAgentHooksMixin, AgentMiddleware):
    @staticmethod
    def _strip_image_url_from_content(content: Any) -> str:
        """Extract text from a multimodal content list, stripping image_url items.

        Returns the concatenated text content. Handles str, dict, and list formats.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return content.get("text", "")
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
            return "\n".join(texts)
        return ""

    # ------------------------------------------------------------------
    # before_agent implementation (shared by sync + async)
    # ------------------------------------------------------------------
    def _before_agent_impl(self, state: AgentState) -> None:
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)
        if not is_safe_session_id(session_id):
            err_text = f"Unsafe session_id: {session_id!r}"
            logger.error(err_text)
            raise RuntimeError(err_text)

        state_mes_list: list[BaseMessage] = state["messages"]
        last_mes: BaseMessage = state_mes_list[-1]

        if not isinstance(last_mes, HumanMessage):
            return

        content: str | dict[str, Any] | list[dict[str, Any]] = getattr(last_mes, "content", None)

        if not isinstance(content, list):
            return

        # These flags describe only the current turn's native attempt; a stale
        # value from an earlier turn must never authorize a fallback.
        state_register_mem.delete_state(session_id, MULTIMODAL_TRYING_NATIVE_KEY)
        state_register_mem.delete_state(session_id, MULTIMODAL_NATIVE_MODEL_KEY)

        # Phase 1 — always persist the uploaded media (decode/save payloads) and
        # collect their paths, independently of the native-vs-skill decision.
        text_dict: dict[str, Any] | None = None
        paths = MediaPaths()

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                if text_dict is not None:
                    raise Exception("Only one text item allowed per input list")
                text_dict = item
                continue
            handler = _MEDIA_HANDLERS.get(item.get("type"))
            if handler is not None:
                handler.process(item, session_id, paths, SRC_DIR)

        if text_dict is None:
            text_dict = {"type": "text", "text": ""}

        if paths.skipped:
            notices = "\n".join(paths.skipped)
            existing_text = str(text_dict.get("text") or "")
            text_dict["text"] = f"{existing_text}\n{notices}" if existing_text else notices

        if not (paths.image_hints or paths.audios or paths.videos):
            # Pure-text list: normalize to a single text block and keep
            # stripping stale image_url blocks from history (existing behavior).
            last_mes.content = [text_dict]
            self._strip_history_images(state_mes_list)
            return

        turn = _TurnMedia(state_mes_list, last_mes, text_dict, paths)
        native_mode = MEDIA_PIPELINE.get("main_llm_native_multimodal", "auto")

        # Phase 2 — explicit user config wins; "auto" consults the process-level
        # capability cache and keeps the blocks unless a family is unsupported.
        if native_mode == "true" or (
            native_mode == "auto" and self._should_keep_native(paths, session_id)
        ):
            self._persist_media_kwargs(last_mes, paths)
            return

        self._apply_skill_path(turn)

    def _should_keep_native(self, paths: MediaPaths, session_id: str) -> bool:
        """Auto mode: decide whether the media blocks stay for a native call.

        A message whose present families are all known-unsupported takes the
        whole skill path. Otherwise the blocks stay: a mixed message (some
        families supported, some unsupported) keeps its native blocks and the
        per-request scrub replaces only the unsupported ones, while an unprobed
        family records the per-turn native-attempt flags so LLMRetry can write
        the cache and fall back when the model rejects the blocks.
        """
        model_key = get_model_key()
        provider, _, model = model_key.partition("/")
        families: tuple[tuple[bool, MediaType], ...] = (
            (bool(paths.image_hints), "vision"),
            (bool(paths.audios), "audio"),
            (bool(paths.videos), "video"),
        )
        present: list[MediaType] = [media for is_present, media in families if is_present]
        capabilities = {media: get_capability(provider, model, media) for media in present}
        if present and all(cap == "unsupported" for cap in capabilities.values()):
            return False
        if any(cap == "auto" for cap in capabilities.values()):
            state_register_mem.set_state(session_id, MULTIMODAL_TRYING_NATIVE_KEY, True)
            state_register_mem.set_state(session_id, MULTIMODAL_NATIVE_MODEL_KEY, model_key)
        return True

    # ------------------------------------------------------------------
    # per-request capability scrub (wrap_model_call)
    # ------------------------------------------------------------------
    def _scrub_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Replace unsupported media blocks in the request copy only.

        The serving model key is the env main-model key: this layer wraps
        outside ``LLMRetryMiddleware``, so a sticky fallback candidate rebound
        deeper in the chain is not observable here. Every media block whose
        family is cached ``"unsupported"`` for that key becomes a text
        placeholder; supported and unprobed blocks pass through untouched. When
        nothing changes the original request object is returned.
        """
        if MEDIA_PIPELINE.get("main_llm_native_multimodal", "auto") != "auto":
            return request
        model_key = get_model_key()
        provider, _, model = model_key.partition("/")
        scrubbed, changed = scrub_messages(list(request.messages), provider, model, model_key)
        if not changed:
            return request
        return request.override(messages=scrubbed)

    def _apply_skill_path(self, turn: _TurnMedia) -> None:
        attach_media_hints(turn.text_dict, turn.paths)
        turn.last_mes.content = [turn.text_dict]
        self._persist_media_kwargs(turn.last_mes, turn.paths)
        self._strip_history_images(turn.state_mes_list)

    def _strip_history_images(self, state_mes_list: list[BaseMessage]) -> None:
        """Strip image_url blocks from history messages. The cheap pre-check
        avoids extracting/assigning on messages with nothing to strip, and the
        assignment only happens when the stripped text is non-empty (same
        semantics as before, minus the needless rewrite)."""
        for mes in state_mes_list[:-1]:
            if not isinstance(mes, HumanMessage):
                continue
            mes_content = getattr(mes, "content", None)
            if not isinstance(mes_content, list):
                continue
            has_image_url = any(
                isinstance(item, dict) and item.get("type") == "image_url" for item in mes_content
            )
            if not has_image_url:
                continue
            text_only = self._strip_image_url_from_content(mes_content)
            if text_only and text_only != mes_content:
                mes.content = text_only

    @staticmethod
    def _persist_media_kwargs(last_mes: HumanMessage, paths: MediaPaths) -> None:
        """Persist the list of media file paths into additional_kwargs so the
        context engine can save them to the messages table for history rendering."""
        if len(paths.persisted_images) > 0 or len(paths.audios) > 0 or len(paths.videos) > 0:
            additional_kwargs: dict[str, Any] = dict(
                getattr(last_mes, "additional_kwargs", {}) or {}
            )
            if len(paths.persisted_images) > 0:
                additional_kwargs["images"] = paths.persisted_images
            if len(paths.audios) > 0:
                additional_kwargs["audios"] = paths.audios
            if len(paths.videos) > 0:
                additional_kwargs["videos"] = paths.videos
            last_mes.additional_kwargs = additional_kwargs

    # ------------------------------------------------------------------
    # after_agent implementation (shared by sync + async)
    # ------------------------------------------------------------------
    def _after_agent_impl(self, state: AgentState) -> None:
        # Clean up cached image files: delete if filename is not a pure numeric timestamp; delete if older than 7 days
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)
        if not is_safe_session_id(session_id):
            err_text = f"Unsafe session_id: {session_id!r}"
            logger.error(err_text)
            raise RuntimeError(err_text)

        temp_dir = SRC_DIR / session_id / "mutil_temp"
        if not temp_dir.exists():
            return

        now_ms: int = int(time.time() * 1000)
        # Retention is configured in days; convert to the millisecond deadline unit.
        seven_days_ms: int = MEDIA_PIPELINE["multimodal_temp_retention_days"] * 24 * 60 * 60 * 1000
        deadline_ms: int = now_ms - seven_days_ms

        deleted_count: int = 0
        for fpath in temp_dir.iterdir():
            if not fpath.is_file():
                continue
            stem: str = fpath.stem  # Filename without extension
            if not stem.isdigit():
                # Filename is tampered with or not in timestamp format, delete directly
                fpath.unlink()
                deleted_count += 1
                continue
            file_time_ms: int = int(stem)
            if file_time_ms < deadline_ms:
                fpath.unlink()
                deleted_count += 1

        if deleted_count > 0:
            logger.debug(f"Cleaned up {deleted_count} expired cached images")

    # ------------------------------------------------------------------
    # before_agent / abefore_agent
    # ------------------------------------------------------------------
    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.debug("{} before_agent hook fired", type(self).__name__)
        self._before_agent_impl(state)
        return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.debug("{} abefore_agent hook fired", type(self).__name__)
        self._before_agent_impl(state)
        return None

    # ------------------------------------------------------------------
    # wrap_model_call / awrap_model_call
    # ------------------------------------------------------------------
    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} wrap_model_call hook fired", type(self).__name__)
        return handler(self._scrub_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} awrap_model_call hook fired", type(self).__name__)
        return await handler(self._scrub_request(request))

    # ------------------------------------------------------------------
    # after_agent / aafter_agent
    # ------------------------------------------------------------------
    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.debug("{} after_agent hook fired", type(self).__name__)
        self._after_agent_impl(state)
        return None

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.debug("{} aafter_agent hook fired", type(self).__name__)
        self._after_agent_impl(state)
        return None
