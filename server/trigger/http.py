import json
from loguru import logger
from requests import Response
from type import MultiModalMessage
from runtime import relation_register
from typing import Any, Dict, Callable
from robyn.status_codes import HTTP_500_INTERNAL_SERVER_ERROR
from ..service import (async_generate, session_end, clear_session, read_system_prompt_file, write_system_prompt_file,
                       update_system_prompt_file, read_character, write_character, update_character)
from robyn import Robyn, SSEMessage, SSEResponse, WebSocketDisconnect, WebSocketAdapter

# 创建app
app = Robyn(__file__)

@app.exception
def handle_session_end(error: Exception)-> Response:
    """
        全局异常拦截器
        当路由函数中抛出任何未被捕获的异常时，此函数会被调用
        """
    # 记录错误日志，便于调试
    logger.exception(error)

    # 返回统一的 JSON 错误格式
    return Response(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        headers={"Content-Type": "application/json"},
        description=json.dumps({
            "success": False,
            "message": "Internal Server Error",
            "error": str(error)
        }, ensure_ascii=False)
    )

@logger.catch
@app.post("/sessions/agent/sse")
async def stream_async_events_handler(request):
    request_json = request.json()

    session_id:str = request_json.get("session_id", None)
    if not session_id:
        return SSEMessage("请提供会话ID")

    multi_modal_message:MultiModalMessage = request_json.get("multi_modal_message", None)
    if not multi_modal_message:
        return SSEMessage("请提供用户输入")
    multi_modal_message = MultiModalMessage(**multi_modal_message)

    return SSEResponse(async_generate(session_id, multi_modal_message))



ws_event_processor_dict: Dict[str, Callable[[str, str | dict[str, Any]], str]] = {}

async def ws_processor(session_id: str, event:str, content: str | dict[str, Any])->Any:
    try:
        processor: Callable[[str, str | dict[str, Any]], str] | None = ws_event_processor_dict.get(event, None)
        if processor is None:
            return None

        return processor(session_id, content)
    except Exception as e:
        logger.warning(f"ws_processor error happened: {e}")
        return None

@app.websocket("/sessions/ws")
async def ws_handler(websocket: WebSocketAdapter):
    try:
        while True:
            try:
                msg: str = await websocket.receive_text()
                obj: dict[str, Any] = json.loads(msg)
                session_id: str = obj.get("session_id", None)
                if session_id is None:
                    continue

                event: str | None = obj.get("event", None)
                if event is None:
                    continue

                content: str | dict[str, Any] | None = obj.get("content", None)
                if content is None:
                    continue

                res: Any = await ws_processor(session_id=session_id, event=event, content=content)

                await websocket.send_text(json.dumps(res))

            except Exception as e:
                logger.warning(f"Error in ws_handler: {e}")
    except (WebSocketDisconnect, ConnectionResetError, Exception) as e:
        logger.warning(f"Client {websocket.id} disconnected: {e}")

@ws_handler.on_connect
async def on_connect(websocket: WebSocketAdapter):
    logger.info(f"Client {websocket.id} connected")

    query_params = websocket.query_params
    session_id: str = query_params.get("session_id", None)
    if session_id is None:
        await websocket.close()
        relation_register.unregister_websocket_by_websocket_id(websocket_id=websocket.id)

    res = {"content": "websocket连接成功"}
    await websocket.send_text(json.dumps(res))

    relation_register.register_websocket(session_id=session_id, websocket=websocket)

@ws_handler.on_close
async def handle_disconnect(websocket: WebSocketAdapter):
    logger.info(f"Client {websocket.id} disconnected")

    # 用户关闭session时弹出并清除session_id
    session_id: str | None = relation_register.get_session_id_by_websocket_id(websocket_id=websocket.id)

    if session_id:
        relation_register.unregister_websocket_by_websocket_id(websocket_id=websocket.id)

        # 执行 关闭session 钩子
        await session_end(session_id = session_id)


@app.delete("/sessions")
async def clear_session_handler(request):
    request_json = request.json()

    session_id: str = request_json.get("session_id", None)
    await clear_session(session_id = session_id)

@app.get("/system_prompt")
async def read_system_prompt_handler(request):
    """
    读取系统提示文件
    """

    return read_system_prompt_file()

@app.put("/system_prompt")
async def write_system_prompt_file_handler(request):
    """
    读取系统提示文件
    """
    request_json = request.json()

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    return write_system_prompt_file(file_to_content)


@app.patch("/system_prompt")
async def update_system_prompt_file_handler(request):
    """
    更新系统提示文件
    """
    request_json = request.json()

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    return update_system_prompt_file(file_to_content)


@app.get("/character")
async def read_character_handler(request):
    """
    读取角色信息
    """

    return read_character()

@app.put("/character")
async def write_character_handler(request):
    """
    写入角色信息
    """
    request_json = request.json()

    character_data: dict[str, dict[str, str]] = request_json.get("character_data", {})
    return write_character(character_data)

@app.patch("/character")
async def update_character_handler(request):
    """
    更新角色信息
    """
    request_json = request.json()

    character_data: dict[str, dict[str, str]] = request_json.get("character_data", {})
    return update_character(character_data)