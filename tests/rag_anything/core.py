import os
import sys
from logging import getLogger
from raganything import RAGAnything

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from models import vl_model
from tests.rag_anything.base import get_lightrag
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage

logger = getLogger(__name__)

async def _vision_model_func(
        prompt: str,
        system_prompt: str | None = None,
        image_data: bytes | str | None = None,
        **kwargs,
) -> str:
    user_content: list[dict] = [{"type": "text", "text": prompt}]
    if image_data is not None:
        b64 = image_data
        if isinstance(b64, bytes):
            b64 = b64.decode("utf-8")
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    messages: list[BaseMessage] = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))

    human_message: HumanMessage = HumanMessage(content=user_content)
    messages.append(human_message)
    result = vl_model.invoke(messages)

    return result.content

_rag_anything: RAGAnything | None = None
async def get_rag_anything()-> RAGAnything:
    global _rag_anything

    if _rag_anything is None:
        lightrag = await get_lightrag()

        _rag_anything = RAGAnything(
            lightrag=lightrag,
            vision_model_func=_vision_model_func
        )

    return _rag_anything