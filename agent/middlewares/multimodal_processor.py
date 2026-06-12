import io
import time
import base64
from PIL import Image
from typing import Any
from loguru import logger
from config import SRC_DIR
from langgraph.runtime import Runtime
from langchain_core.messages import BaseMessage, HumanMessage
from langchain.agents.middleware import AgentMiddleware, AgentState

class MultimodalProcessor(AgentMiddleware):
    def __init__(self, session_id: str):
        super().__init__()
        self._session_id: str = session_id

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        state_mes_list: list[BaseMessage] = state["messages"]
        last_mes: BaseMessage = state_mes_list[-1]

        if not isinstance(last_mes, HumanMessage):
            return None

        content: str | dict[str, Any] | list[dict[str, Any]] = getattr(last_mes, "content", None)

        if not isinstance(content, list):
            return None

        text_dict: dict[str, Any] | None = None
        image_path_list: list[str] = []

        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    if text_dict is not None:
                        raise Exception("一个输入列表中不能type='text'类型数据只能存在一个")
                    text_dict = item

                elif item.get("type") == "image_url":
                    data_url: str = item.get("image_url", {}).get("url", "")
                    # 判断是否已经有data URI前缀
                    if data_url.startswith('data:image/'):
                        # 已有前缀，直接使用
                        base64_data: str = data_url.split(",")[1]
                    else:
                        # 没有前缀，添加前缀
                        base64_data: str = data_url

                    image_bytes = base64.b64decode(base64_data)
                    image = Image.open(io.BytesIO(image_bytes))

                    temp_dir = SRC_DIR / "mutil_temp"
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    temp_path = temp_dir / f"{str(int(time.time() * 1000))}.png"
                    temp_path = temp_path.resolve()
                    image.save(temp_path, "PNG")
                    logger.info("图片缓存成功！")

                    image_path_list.append(temp_path.as_posix())

        if text_dict is None:
            text_dict = {"type": "text", "text": ""}

        if len(image_path_list) > 0:
            text_dict["text"] += f"[System: The user uploaded {len(image_path_list)} image(s). Location: {",".join(image_path_list)}. If you need to view the image(s), use the image_to_text skill.]"
            last_mes.content = [text_dict]

    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        # 清理缓存图片文件：文件名不是纯数字时间戳 → 直接删；超过7天的 → 删
        temp_dir = SRC_DIR / "mutil_temp"
        if not temp_dir.exists():
            return None

        now_ms: int = int(time.time() * 1000)
        seven_days_ms: int = 7 * 24 * 60 * 60 * 1000
        deadline_ms: int = now_ms - seven_days_ms

        deleted_count: int = 0
        for fpath in temp_dir.iterdir():
            if not fpath.is_file():
                continue
            stem: str = fpath.stem  # 文件名（不含扩展名）
            if not stem.isdigit():
                # 文件名被篡改或不符合时间戳格式，直接删除
                fpath.unlink()
                deleted_count += 1
                continue
            file_time_ms: int = int(stem)
            if file_time_ms < deadline_ms:
                fpath.unlink()
                deleted_count += 1

        if deleted_count > 0:
            logger.info(f"清理了{deleted_count}个过期缓存图片")