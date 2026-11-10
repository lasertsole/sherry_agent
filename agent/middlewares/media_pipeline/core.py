import asyncio
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
from ..base import AfterAgentHooksMixin, BeforeAgentHooksMixin
from .media_handlers import MediaPaths, MediaType, _MEDIA_HANDLERS
from .scrub import scrub_messages
from pub.func.message.classify import is_user_message
from pub.func.validator import is_safe_session_id
from runtime import StateKey, state_register_mem

MULTIMODAL_TRYING_NATIVE_KEY = StateKey.MULTIMODAL_TRYING_NATIVE
MULTIMODAL_NATIVE_MODEL_KEY = StateKey.MULTIMODAL_NATIVE_MODEL

_MEDIA_OMITTED_PLACEHOLDER = (
    "[Attached image]\n"
    "[Media omitted from provider request to keep the request body under the "
    "configured media budget.]"
)


def _data_url_bytes(url: str) -> int:
    """Decoded payload size of a base64 data URL (0 for a remote URL).

    The base64 length formula avoids decoding megabytes of payload on every
    model call; a non-base64 data URL falls back to its UTF-8 length.
    """
    if not url.startswith("data:"):
        return 0
    header, separator, payload = url.partition(",")
    if not separator:
        return 0
    payload = payload.strip()
    if ";base64" not in header:
        return len(payload.encode("utf-8"))
    padding = len(payload) - len(payload.rstrip("="))
    return max(0, len(payload) * 3 // 4 - padding)


def _image_block_bytes(item: dict[str, Any]) -> int | None:
    """Byte cost of one content block, or None when it is not an image."""
    if item.get("type") != "image_url":
        return None
    image_url = item.get("image_url")
    if isinstance(image_url, dict):
        url = image_url.get("url", "")
    else:
        url = image_url if isinstance(image_url, str) else ""
    return _data_url_bytes(url) if isinstance(url, str) else 0


@dataclass
class _TurnMedia:
    """The media state of one processed turn: the last message, its text block
    (media hints are appended here) and the paths collected while persisting."""

    state_mes_list: list[BaseMessage]
    last_mes: HumanMessage
    text_dict: dict[str, Any]
    paths: MediaPaths


class MultimodalProcessor(BeforeAgentHooksMixin, AfterAgentHooksMixin, AgentMiddleware):
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

        if not (paths.image_hints or paths.audios or paths.videos or paths.pdf_present):
            # Pure-text list: normalize to a single text block. History media is
            # projected against the per-request byte budget in wrap_model_call.
            last_mes.content = [text_dict]
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
            (bool(paths.pdf_present), "pdf"),
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
        if turn.paths.pdf_present:
            notice = (
                "[Uploaded media] The attached PDF was not sent: this model does not "
                "support native PDF input and there is no PDF skill fallback."
            )
            existing = str(turn.text_dict.get("text") or "")
            turn.text_dict["text"] = f"{existing}\n{notice}" if existing else notice
        attach_media_hints(turn.text_dict, turn.paths)
        turn.last_mes.content = [turn.text_dict]
        self._persist_media_kwargs(turn.last_mes, turn.paths)

    # ------------------------------------------------------------------
    # per-request history media budget (wrap_model_call)
    # ------------------------------------------------------------------
    def _project_history_media_budget(
        self,
        messages: list[BaseMessage],
        *,
        budget_bytes: int | None = None,
        preserve_latest: bool | None = None,
    ) -> list[BaseMessage]:
        """Keep history media newest-first within the byte budget.

        Runs after the capability scrub, on the request copy only. Every
        ``image_url`` block is measured by its decoded data-URL size (a remote
        URL is fetched by the provider, so it costs nothing and is kept), the
        latest real user message's media is protected, and blocks that no
        longer fit are replaced by a text placeholder. This replaces the old
        unconditional strip: a session within budget keeps all of its images,
        while an oversized one keeps the newest and drops the oldest. A budget
        of ``0`` drops every block (the explicit skill-path mode).
        """
        budget = int(
            budget_bytes
            if budget_bytes is not None
            else MEDIA_PIPELINE.get("history_media_budget_bytes", 0) or 0
        )
        if budget < 0:
            return messages
        preserve = bool(
            preserve_latest
            if preserve_latest is not None
            else MEDIA_PIPELINE.get("preserve_latest_user_media", True)
        )
        protected_index = self._latest_media_user_index(messages) if preserve else -1

        blocks: list[tuple[int, int, int, bool]] = []
        for message_index, message in enumerate(messages):
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                continue
            for item_index, item in enumerate(content):
                if not isinstance(item, dict):
                    continue
                size = _image_block_bytes(item)
                if size is None:
                    continue
                blocks.append((message_index, item_index, size, message_index == protected_index))
        if not blocks:
            return messages

        protected_bytes = sum(size for *_, size, protected in blocks if protected)
        remaining = max(0, budget - protected_bytes)
        used = 0
        dropped: set[tuple[int, int]] = set()
        for message_index, item_index, size, protected in sorted(
            (block for block in blocks if not block[3]),
            key=lambda block: block[0],
            reverse=True,
        ):
            if used + size <= remaining:
                used += size
                continue
            dropped.add((message_index, item_index))
        if not dropped:
            return messages

        projected: list[BaseMessage] = []
        for message_index, message in enumerate(messages):
            if not any(index == message_index for index, _ in dropped):
                projected.append(message)
                continue
            content = getattr(message, "content", None)
            assert isinstance(content, list)
            projected.append(
                message.model_copy(
                    update={
                        "content": [
                            {"type": "text", "text": _MEDIA_OMITTED_PLACEHOLDER}
                            if (message_index, item_index) in dropped
                            else item
                            for item_index, item in enumerate(content)
                        ]
                    }
                )
            )
        return projected

    @staticmethod
    def _latest_media_user_index(messages: list[BaseMessage]) -> int:
        """Index of the latest real user message, else the newest media Human.

        ``metadata.origin == "user"`` is the authoritative marker. A transcript
        without it (a compacted/resumed history, a unit test) falls back to the
        newest human message that carries media, which is the same message for
        every shape the server produces.
        """
        for index in range(len(messages) - 1, -1, -1):
            if is_user_message(messages[index]):
                return index
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, HumanMessage):
                continue
            content = getattr(message, "content", None)
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "image_url" for item in content
            ):
                return index
        return -1

    def _project_budget_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        messages = list(request.messages)
        mode = MEDIA_PIPELINE.get("main_llm_native_multimodal", "auto")
        if mode in ("auto", "true"):
            projected = self._project_history_media_budget(messages)
        else:
            # "false"/unknown is the explicit skill path: no media block may
            # ride a request, including one left over from an earlier turn.
            projected = self._project_history_media_budget(
                messages, budget_bytes=0, preserve_latest=False
            )
        if projected is messages:
            return request
        return request.override(messages=projected)

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
        logger.bind(middleware=type(self).__name__).debug("before_agent hook fired")
        self._before_agent_impl(state)
        return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.bind(middleware=type(self).__name__).debug("abefore_agent hook fired")
        # The impl downloads media (urlopen), decodes images and writes files;
        # run it off the loop so one large upload cannot stall every other
        # session's stream.
        await asyncio.to_thread(self._before_agent_impl, state)
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
        logger.bind(middleware=type(self).__name__).debug("wrap_model_call hook fired")
        return handler(self._project_budget_request(self._scrub_request(request)))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.bind(middleware=type(self).__name__).debug("awrap_model_call hook fired")
        return await handler(self._project_budget_request(self._scrub_request(request)))

    # ------------------------------------------------------------------
    # after_agent / aafter_agent
    # ------------------------------------------------------------------
    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.bind(middleware=type(self).__name__).debug("after_agent hook fired")
        self._after_agent_impl(state)
        return None

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        logger.bind(middleware=type(self).__name__).debug("aafter_agent hook fired")
        # Cache cleanup walks the media directories (iterdir/unlink); keep that
        # filesystem work off the event loop like the before hook.
        await asyncio.to_thread(self._after_agent_impl, state)
        return None
