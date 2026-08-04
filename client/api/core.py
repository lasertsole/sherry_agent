import json
import requests
from loguru import logger
from config import API_HOST, API_PORT
from typing import AsyncGenerator, Any
from websockets.asyncio.client import connect

async def post_agent_astream(request_json: dict[str, Any]) -> AsyncGenerator[tuple[str | None, str], None]:
    uri = f"ws://{API_HOST}:{API_PORT}/sessions/agent/ws"
    async with connect(uri) as ws:
        await ws.send(json.dumps(request_json))
        async for raw_msg in ws:
            data = json.loads(raw_msg)
            event = data.get("event", "")
            session_id = data.get("session_id")
            content = data.get("content", "")
            if event == "chunk":
                yield session_id, content
            elif event == "done":
                break
            elif event == "error":
                logger.error(f"Agent WS error: session_id={session_id}, content={content}")
                break

def stop_agent(session_id: str) -> None:
    with requests.post(f"http://{API_HOST}:{API_PORT}/sessions/agent/stop", json={"session_id": session_id}) as response:
        if not response.status_code == 200:
            logger.warning(f"Stop agent failed: {response.text}")

def clear_session(request_json: dict[str, Any])-> tuple[bool, str|None]:
    with requests.delete(f"http://{API_HOST}:{API_PORT}/sessions", json=request_json) as response:
        if response.status_code == 200:
            return True, None

        return False, response.text