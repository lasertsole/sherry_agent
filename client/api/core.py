import requests
from pub_func import process_sse_data
from config import API_HOST, API_PORT
from typing import AsyncGenerator, Any, Tuple

async def post_agent_astream(request_json: dict[str, Any]) -> AsyncGenerator[str, None]:
    with requests.post(f"http://{API_HOST}:{API_PORT}/sessions/agent/sse", stream=True, json=request_json) as response:
        for line in response.iter_lines():
            yield process_sse_data(line)


def clear_session(request_json: dict[str, Any])-> Tuple[bool, str|None]:
    with requests.delete(f"http://{API_HOST}:{API_PORT}/sessions", json=request_json) as response:
        if response.status_code == 200:
            return True, None

        return False, response.text