import base64
from typing import Any, List
from pub_func import generate_tsid
from .chats_storage import ChatStorage
from config import USER_NAME, ASSISTANT_NAME
from type import MultiModalMessage, File, FileType

def storage_add_chat(session_id: str, role: str, multi_modal_message: MultiModalMessage):
    name: str = USER_NAME if role == "user" else ASSISTANT_NAME
    content = multi_modal_message.text

    # 初始化聊天记录存储类
    chat_storage: ChatStorage = ChatStorage(session_id=session_id, chats_maxlen=20)

    # 生成时间戳
    timestamp:str = generate_tsid()

    # 添加聊天记录到显示列表
    files: list[File] = []
    image_base64_list: List[str] = multi_modal_message.image_base64_list
    if image_base64_list is not None:
        for image_base64 in image_base64_list:
            image_bytes = base64.b64decode(image_base64.encode("utf-8"))
            file: File = {"content": image_bytes, "type": FileType.IMAGE, "extension": '.jpg'}
            files.append(file)

    audio_bytes_list: List[bytes] = multi_modal_message.audio_bytes_list
    if audio_bytes_list is not None:
        for audio_bytes in audio_bytes_list:
            file: File = {"content": audio_bytes, "type": FileType.AUDIO, "extension": '.wav'}
            files.append(file)


    chat: dict[str, Any] = {"role": role, "content": f"{name}:{content}", "timestamp": timestamp}

    chat_storage.add_chat(new_chat = chat, files = files)