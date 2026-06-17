from loguru import logger
from .core import Register
from robyn import WebSocketAdapter

class RelationManager(Register):
    def __init__(self):
        if self._initialized:
            return

        # websocket
        self.websocket_id_to_session_id: dict[str, str] = {}
        self.session_id_to_websocket_id: dict[str, str] = {}
        self.websocket_id_to_ws: dict[str, WebSocketAdapter] = {}

        # channel
        self.session_id_to_channel_chat_id: dict[str, tuple[str, str]] = {}
        self.channel_chat_id_to_session_id: dict[tuple[str, str], str] = {}

        self._initialized = True

    """
        websocket
    """
    def register_websocket(self, session_id: str, websocket: WebSocketAdapter):
        try:
            self.websocket_id_to_session_id[websocket.id] = session_id
            self.session_id_to_websocket_id[session_id] = websocket.id
            self.websocket_id_to_ws[websocket.id] = websocket
        except Exception:
            logger.exception(f"register_websocket failed: session_id={session_id}")

    def unregister_websocket_by_websocket(self, websocket: WebSocketAdapter):
        try:
            session_id: str = self.websocket_id_to_session_id.pop(websocket.id, None)
            if session_id:
                self.session_id_to_websocket_id.pop(session_id, None)
                self.websocket_id_to_ws.pop(websocket.id, None)
        except Exception:
            logger.exception(f"unregister_websocket_by_websocket failed")

    def unregister_websocket_by_websocket_id(self, websocket_id: str):
        try:
            self.websocket_id_to_ws.pop(websocket_id, None)
            session_id: str  = self.websocket_id_to_session_id.pop(websocket_id, None)
            if session_id:
                self.session_id_to_websocket_id.pop(session_id, None)
        except Exception:
            logger.exception(f"unregister_websocket_by_websocket_id failed: websocket_id={websocket_id}")

    def unregister_websocket_by_session_id(self, session_id: str):
        try:
            websocket_id: str  = self.session_id_to_websocket_id.pop(session_id, None)
            self.websocket_id_to_session_id.pop(websocket_id, None)
            if websocket_id:
                self.websocket_id_to_ws.pop(websocket_id, None)
        except Exception:
            logger.exception(f"unregister_websocket_by_session_id failed: session_id={session_id}")

    def get_websocket_by_session_id(self, session_id: str)->WebSocketAdapter | None:
        try:
            websocket_id: str = self.session_id_to_websocket_id.get(session_id, None)
            if websocket_id:
                return self.websocket_id_to_ws.get(websocket_id, None)
        except Exception:
            logger.exception(f"get_websocket_by_session_id failed: session_id={session_id}")
        return None

    def get_websocket_by_websocket_id(self, websocket_id: str) -> WebSocketAdapter | None:
        try:
            return self.websocket_id_to_ws.get(websocket_id, None)
        except Exception:
            logger.exception(f"get_websocket_by_websocket_id failed: websocket_id={websocket_id}")
        return None

    def get_session_id_by_websocket_id(self, websocket_id: str)->str | None:
        try:
            return self.websocket_id_to_session_id.get(websocket_id, None)
        except Exception:
            logger.exception(f"get_session_id_by_websocket_id failed: websocket_id={websocket_id}")
        return None

    def get_session_id_by_websocket(self, websocket: WebSocketAdapter)->str | None:
        return self.get_session_id_by_websocket_id(websocket.id)

    def get_websocket_id_by_session_id(self, session_id: str)->str | None:
        try:
            return self.session_id_to_websocket_id.get(session_id, None)
        except Exception:
            logger.exception(f"get_websocket_id_by_session_id failed: session_id={session_id}")
        return None


    """
        channel
    """
    def register_channel_chat(self, session_id: str, channel_id: str, chat_id: str):
        try:
            self.session_id_to_channel_chat_id[session_id] = (channel_id, chat_id)
            self.channel_chat_id_to_session_id[(channel_id, chat_id)] = session_id
        except Exception:
            logger.exception(f"register_channel_chat failed: session_id={session_id}")

    def unregister_channel_chat_by_session_id(self, session_id: str):
        try:
            channel_id, chat_id = self.session_id_to_channel_chat_id.pop(session_id, (None, None))
            if channel_id and chat_id:
                self.channel_chat_id_to_session_id.pop((channel_id, chat_id), None)
        except Exception:
            logger.exception(f"unregister_channel_chat_by_session_id failed: session_id={session_id}")

    def unregister_channel_chat_by_channel_chat_id(self, channel_id: str, chat_id: str):
        try:
            session_id: str = self.channel_chat_id_to_session_id.pop((channel_id, chat_id), None)
            if session_id:
                self.session_id_to_channel_chat_id.pop(session_id, None)
        except Exception:
            logger.exception(f"unregister_channel_chat_by_channel_chat_id failed: channel_id={channel_id}, chat_id={chat_id}")

    def get_session_id_by_channel_chat_id(self, channel_id: str, chat_id: str)->str | None:
        try:
            return self.channel_chat_id_to_session_id.get((channel_id, chat_id), None)
        except Exception:
            logger.exception(f"get_session_id_by_channel_chat_id failed: channel_id={channel_id}, chat_id={chat_id}")
        return None

    def get_channel_chat_id_by_session_id(self, session_id: str)->tuple[str, str] | None:
        try:
            return self.session_id_to_channel_chat_id.get(session_id, None)
        except Exception:
            logger.exception(f"get_channel_chat_id_by_session_id failed: session_id={session_id}")
        return None

    def clear_session(self, session_id: str):
        try:
            self.unregister_websocket_by_session_id(session_id)
            self.unregister_websocket_by_websocket_id(session_id)
        except Exception:
            logger.exception(f"clear_session failed: session_id={session_id}")

relation_register = RelationManager()