from loguru import logger
import threading
from collections import OrderedDict
from .core import SessionRegister
from robyn import WebSocketAdapter


class RelationManager(SessionRegister):
    def __init__(self):
        if self._initialized:
            return

        # One lock for all five mappings: every register/unregister updates
        # TWO dicts, and a torn pair breaks reverse lookups.
        # RLock: clear_session re-enters it via unregister_* helpers.
        self._rm_lock = threading.RLock()

        # websocket
        self.websocket_id_to_session_id: dict[str, str] = {}
        self.session_id_to_websocket_id: dict[str, str] = {}
        self.websocket_id_to_ws: dict[str, WebSocketAdapter] = {}
        # Every live socket of a session, oldest -> newest. The 1:1 map above
        # stays as the "newest socket" view for callers that need one; pushes
        # fan out over this bag (see runtime.session.ws_push.broadcast_to_session).
        self.session_id_to_websocket_ids: dict[str, OrderedDict[str, WebSocketAdapter]] = {}

        # channel
        self.session_id_to_channel_chat_id: dict[str, tuple[str, str]] = {}
        self.channel_chat_id_to_session_id: dict[tuple[str, str], str] = {}

        self._initialized = True

    """
        websocket
    """

    def register_websocket(self, session_id: str, websocket: WebSocketAdapter):
        try:
            with self._rm_lock:
                self.websocket_id_to_session_id[websocket.id] = session_id
                self.session_id_to_websocket_id[session_id] = websocket.id
                self.websocket_id_to_ws[websocket.id] = websocket
                bag = self.session_id_to_websocket_ids.setdefault(session_id, OrderedDict())
                # Re-registering the same socket must not duplicate the bag
                # entry (it would be delivered twice by a broadcast).
                bag.pop(websocket.id, None)
                bag[websocket.id] = websocket
        except Exception:
            logger.exception(f"register_websocket failed: session_id={session_id}")

    def unregister_websocket_by_websocket(self, websocket: WebSocketAdapter):
        try:
            with self._rm_lock:
                session_id: str = self.websocket_id_to_session_id.pop(websocket.id, None)
                self.websocket_id_to_ws.pop(websocket.id, None)
                if session_id:
                    bag = self.session_id_to_websocket_ids.get(session_id)
                    if bag is not None:
                        bag.pop(websocket.id, None)
                        if not bag:
                            self.session_id_to_websocket_ids.pop(session_id, None)
                # Last-writer-wins guard: a newer socket may have re-registered
                # the same session (per-message agent WS pattern). A stale
                # disconnect must only clear the binding it still owns, or the
                # live socket silently loses its streamed frames.
                if session_id and self.session_id_to_websocket_id.get(session_id) == websocket.id:
                    bag = self.session_id_to_websocket_ids.get(session_id)
                    if bag:
                        # Promote the newest surviving socket rather than
                        # leaving the session without a 1:1 target.
                        self.session_id_to_websocket_id[session_id] = next(reversed(bag))
                    else:
                        self.session_id_to_websocket_id.pop(session_id, None)
        except Exception:
            logger.exception("unregister_websocket_by_websocket failed")

    def unregister_websocket_by_websocket_id(self, websocket_id: str):
        try:
            with self._rm_lock:
                self.websocket_id_to_ws.pop(websocket_id, None)
                session_id: str = self.websocket_id_to_session_id.pop(websocket_id, None)
                if session_id:
                    bag = self.session_id_to_websocket_ids.get(session_id)
                    if bag is not None:
                        bag.pop(websocket_id, None)
                        if not bag:
                            self.session_id_to_websocket_ids.pop(session_id, None)
                # Same last-writer-wins guard as unregister_websocket_by_websocket.
                if session_id and self.session_id_to_websocket_id.get(session_id) == websocket_id:
                    bag = self.session_id_to_websocket_ids.get(session_id)
                    if bag:
                        self.session_id_to_websocket_id[session_id] = next(reversed(bag))
                    else:
                        self.session_id_to_websocket_id.pop(session_id, None)
        except Exception:
            logger.exception(
                f"unregister_websocket_by_websocket_id failed: websocket_id={websocket_id}"
            )

    def unregister_websocket_by_session_id(self, session_id: str):
        try:
            with self._rm_lock:
                self.session_id_to_websocket_ids.pop(session_id, None)
                websocket_id: str = self.session_id_to_websocket_id.pop(session_id, None)
                self.websocket_id_to_session_id.pop(websocket_id, None)
                if websocket_id:
                    self.websocket_id_to_ws.pop(websocket_id, None)
                # Any OTHER socket of the same session also loses its socket id
                # mapping (the bag is gone, so broadcasts can no longer reach it).
        except Exception:
            logger.exception(f"unregister_websocket_by_session_id failed: session_id={session_id}")

    def get_websockets_by_session_id(self, session_id: str) -> list[WebSocketAdapter]:
        """Every live socket registered for ``session_id`` (oldest -> newest)."""
        try:
            with self._rm_lock:
                bag = self.session_id_to_websocket_ids.get(session_id)
                return list(bag.values()) if bag else []
        except Exception:
            logger.exception(f"get_websockets_by_session_id failed: session_id={session_id}")
            return []

    def get_all_websockets(self) -> list[WebSocketAdapter]:
        """Every live socket across all sessions (registration order)."""
        try:
            with self._rm_lock:
                return list(self.websocket_id_to_ws.values())
        except Exception:
            logger.exception("get_all_websockets failed")
            return []

    def get_websocket_by_session_id(self, session_id: str) -> WebSocketAdapter | None:
        try:
            with self._rm_lock:
                websocket_id: str = self.session_id_to_websocket_id.get(session_id, None)
                if websocket_id:
                    return self.websocket_id_to_ws.get(websocket_id, None)
        except Exception:
            logger.exception(f"get_websocket_by_session_id failed: session_id={session_id}")
        return None

    def get_websocket_by_websocket_id(self, websocket_id: str) -> WebSocketAdapter | None:
        try:
            with self._rm_lock:
                return self.websocket_id_to_ws.get(websocket_id, None)
        except Exception:
            logger.exception(f"get_websocket_by_websocket_id failed: websocket_id={websocket_id}")
        return None

    def get_session_id_by_websocket_id(self, websocket_id: str) -> str | None:
        try:
            with self._rm_lock:
                return self.websocket_id_to_session_id.get(websocket_id, None)
        except Exception:
            logger.exception(f"get_session_id_by_websocket_id failed: websocket_id={websocket_id}")
        return None

    def get_session_id_by_websocket(self, websocket: WebSocketAdapter) -> str | None:
        return self.get_session_id_by_websocket_id(websocket.id)

    def get_websocket_id_by_session_id(self, session_id: str) -> str | None:
        try:
            with self._rm_lock:
                return self.session_id_to_websocket_id.get(session_id, None)
        except Exception:
            logger.exception(f"get_websocket_id_by_session_id failed: session_id={session_id}")
        return None

    """
        channel
    """

    def register_channel_chat(self, session_id: str, channel_id: str, chat_id: str):
        try:
            with self._rm_lock:
                self.session_id_to_channel_chat_id[session_id] = (channel_id, chat_id)
                self.channel_chat_id_to_session_id[(channel_id, chat_id)] = session_id
        except Exception:
            logger.exception(f"register_channel_chat failed: session_id={session_id}")

    def unregister_channel_chat_by_session_id(self, session_id: str):
        try:
            with self._rm_lock:
                channel_id, chat_id = self.session_id_to_channel_chat_id.pop(
                    session_id, (None, None)
                )
                if channel_id and chat_id:
                    self.channel_chat_id_to_session_id.pop((channel_id, chat_id), None)
        except Exception:
            logger.exception(
                f"unregister_channel_chat_by_session_id failed: session_id={session_id}"
            )

    def unregister_channel_chat_by_channel_chat_id(self, channel_id: str, chat_id: str):
        try:
            with self._rm_lock:
                session_id: str = self.channel_chat_id_to_session_id.pop(
                    (channel_id, chat_id), None
                )
                if session_id:
                    self.session_id_to_channel_chat_id.pop(session_id, None)
        except Exception:
            logger.exception(
                f"unregister_channel_chat_by_channel_chat_id failed: channel_id={channel_id}, chat_id={chat_id}"
            )

    def get_session_id_by_channel_chat_id(self, channel_id: str, chat_id: str) -> str | None:
        try:
            with self._rm_lock:
                return self.channel_chat_id_to_session_id.get((channel_id, chat_id), None)
        except Exception:
            logger.exception(
                f"get_session_id_by_channel_chat_id failed: channel_id={channel_id}, chat_id={chat_id}"
            )
        return None

    def get_channel_chat_id_by_session_id(self, session_id: str) -> tuple[str, str] | None:
        try:
            with self._rm_lock:
                return self.session_id_to_channel_chat_id.get(session_id, None)
        except Exception:
            logger.exception(f"get_channel_chat_id_by_session_id failed: session_id={session_id}")
        return None

    def clear_session(self, session_id: str):
        try:
            with self._rm_lock:
                bag = self.session_id_to_websocket_ids.pop(session_id, None)
                websocket_id = self.session_id_to_websocket_id.get(session_id)
                self.unregister_websocket_by_session_id(session_id)
                if websocket_id:
                    self.unregister_websocket_by_websocket_id(websocket_id)
                for ws_id in bag or ():
                    self.websocket_id_to_session_id.pop(ws_id, None)
                    self.websocket_id_to_ws.pop(ws_id, None)
        except Exception:
            logger.exception(f"clear_session failed: session_id={session_id}")


relation_register = RelationManager()
