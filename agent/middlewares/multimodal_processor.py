import time
from typing import Any
from loguru import logger
from config import SRC_DIR
from typing import override
from langgraph.runtime import Runtime
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, HumanMessage

from agent.middlewares.mixins import BeforeAgentHooksMixin, AfterAgentHooksMixin
from agent.middlewares.media_handlers import MediaPaths, _MEDIA_HANDLERS
from pub.func.validator import is_safe_session_id


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

        self._attach_media_hints(text_dict, paths)
        last_mes.content = [text_dict]
        self._persist_media_kwargs(last_mes, paths)

        # Strip image_url blocks from history messages
        for mes in state_mes_list[:-1]:
            if not isinstance(mes, HumanMessage):
                continue
            mes_content = getattr(mes, "content", None)
            if isinstance(mes_content, list):
                text_only = self._strip_image_url_from_content(mes_content)
                mes.content = text_only if text_only else mes_content

    @staticmethod
    def _attach_media_hints(text_dict: dict[str, Any], paths: MediaPaths) -> None:
        """Attach the media hints directly into the HumanMessage's text block
        (NOT a SystemMessage). The user explicitly required this HumanMessage
        approach — the previous version steered the model this way. Mentioning
        the media location here lets the model know it must invoke the native
        image_to_text TOOL to actually "see" the image, since current model has no
        native vision ability. The persistence layer (add_messages) stores the
        HumanMessage, so the hint remains visible for reasoning within this turn.
        """
        if len(paths.image_hints) > 0:
            text_dict["text"] += (
                f"\n[Uploaded media] The user uploaded {len(paths.image_hints)} image(s). "
                f"Location: {','.join(paths.image_hints)}. "
                "You MUST use skill discovery to recognize this/these image(s): call the "
                "skill_view tool with name 'image_to_text' to read its SKILL.md. Then follow "
                "the skill's script instructions exactly (it tells you how to run the "
                "image_to_text recognition logic via the terminal tool). "
                "current model has no native vision ability, so do NOT answer based on guesswork — "
                "the recognition result reported by the skill script is the ground truth "
                "you must base your reply on."
            )
        if len(paths.audios) > 0:
            text_dict["text"] += (
                f"\n[Uploaded media] The user uploaded {len(paths.audios)} audio(s). "
                f"Location: {','.join(paths.audios)}. "
                "You MUST use skill discovery to transcribe this/these audio(s): call the "
                "skill_view tool with name 'speech_to_text' to read its SKILL.md. Then follow "
                "the skill's script instructions exactly (it tells you how to run the "
                "speech_to_text recognition logic via the terminal tool). "
                "Do NOT answer based on guesswork — the transcription result reported by the "
                "skill script is the ground truth you must base your reply on."
            )
        if len(paths.videos) > 0:
            text_dict["text"] += (
                f"\n[Uploaded media] The user uploaded {len(paths.videos)} video(s). "
                f"Location: {','.join(paths.videos)}. "
                "You MUST use skill discovery to process this/these video(s): call the "
                "skill_view tool with name 'video_text_to_text' to read its SKILL.md. Then follow "
                "the skill's script instructions exactly (it tells you how to run the "
                "video_text_to_text recognition logic via the terminal tool). "
                "Do NOT answer based on guesswork — the recognition result reported by the "
                "skill script is the ground truth you must base your reply on."
            )

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
        seven_days_ms: int = 7 * 24 * 60 * 60 * 1000
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
