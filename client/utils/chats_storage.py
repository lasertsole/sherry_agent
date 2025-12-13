import json
import time
from pathlib import Path
from collections import deque
from type import Chat, File, FileType
from typing import List, Optional, Deque, Any

current_dir = Path(__file__).parent.resolve()
SESSION_FOLDER = (current_dir / '../../src/sessions').resolve()


# 聊天记录存储类
class ChatStorage:
    _session_id: str
    _chats_deque: deque[Chat]
    _chats_storage_file: Path

    def __init__(self, session_id: str, chats_maxlen: int = 20):
        self._session_id = session_id

        # 获取已持久化的聊天记录
        chats_list: List[Chat] = []

        SESSION_FOLDER.mkdir(parents=True, exist_ok=True)
        self._chats_storage_file = (SESSION_FOLDER / f"{self._session_id}.jsonl").resolve()

        if self._chats_storage_file.exists():
            with open(self._chats_storage_file, 'r', encoding='utf-8') as f:
                for line in f:
                    chats_list.append(Chat(**json.loads(line.strip())))

        # 根据时间戳排序聊天记录,从新到旧
        chats_list.sort(key=lambda x: x.timestamp)

        # 将新聊天记录装入双向列表
        self._chats_deque = deque(chats_list[-chats_maxlen:], maxlen=chats_maxlen)

        # 将聊天记录对应的文件删除
        self._delete_file(self._chats_deque)

        # 将新聊天记录列表写回文件
        self.__storage_chats_deque(self._chats_deque)

    def get_session_id(self)->str:
        return self._session_id

    def get_chats(self) -> List[dict[str, Any]]:
        return list([chat.model_dump() for chat in self._chats_deque])

    def _delete_file(self, new_chat_deque: Deque[Chat]):
        # 获取所有多媒体文件夹
        file_type_list:List[str] = [member.value for member in FileType.__members__.values()]

        # 根据文件夹名创建dict类型变量delete_folders_dict
        delete_folders_dict = {folder: [] for folder in file_type_list}

        # 将对于文件夹下的所有文件路径读进delete_folders_dict
        for fileType in file_type_list:
            for file_path in Path(SESSION_FOLDER / fileType).glob("*"):
                delete_folders_dict[file_path.parent.name].append(file_path.as_posix())

        # 如果new_chat_deque内的文件在delete_folders_dict内，则将文件从delete_folders_dict字典内中移除
        for chat in new_chat_deque:
            for file_type in file_type_list:
                file_list: List[str] = getattr(chat, f"{file_type}_path_list")
                if file_list:
                    delete_folders_dict[file_type] = [file_path for file_path in delete_folders_dict[file_type] if file_path not in file_list]

        # 根据delete_folders_dict删除文件
        for file_type in file_type_list:
            for file_path in delete_folders_dict[file_type]:
                try:
                    if Path(file_path).exists():
                        Path(file_path).unlink()
                except Exception as e:
                    pass

    def __storage_chats_deque(self, _chats_deque: Deque[Chat]):
        SESSION_FOLDER.mkdir(parents=True, exist_ok=True)

        # 将新聊天记录列表写回文件
        with open(self._chats_storage_file, 'w', encoding='utf-8') as f:
            for chat in _chats_deque:
                f.write(json.dumps(chat.model_dump(), ensure_ascii=False) + "\n")

    def add_chat(self, new_chat: dict[str, Any], files: Optional[List[File]] = None):
        _new_chat = Chat(**new_chat)
        
        # 将新聊天记录装入双向列表
        self._chats_deque.append(_new_chat)

        # 将文件写入文件夹
        file_path_list = []
        audio_path_list = []
        image_path_list = []
        if files:
            for file in files:
                folder_path = SESSION_FOLDER / file["type"].value
                file_path = folder_path / str(time.time_ns())
                file_path = file_path.as_posix()

                extension = file["extension"]
                match file["type"]:
                    case FileType.AUDIO:
                        file_path = file_path + (extension or '.wav')
                        audio_path_list.append(file_path)
                    case FileType.IMAGE:
                        file_path = file_path + (extension or '.jpg')
                        image_path_list.append(file_path)
                    case _:
                        raise ValueError("Invalid file type")

                # 确保目录存在
                Path(folder_path).mkdir(parents=True, exist_ok=True)

                with open(file_path, "wb") as f:
                    f.write(file["content"])
                    file_path_list.append(file_path)

        # 将文件路径写入新聊天记录
        _new_chat.audio_path_list = audio_path_list
        _new_chat.image_path_list = image_path_list

        # 将聊天记录追加到文件末尾
        with open(self._chats_storage_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(_new_chat.model_dump(), ensure_ascii=False) + "\n")


    def clear_chats(self):
        path = (SESSION_FOLDER / f"{self._session_id}.jsonl").resolve()
        if path.exists():
            path.unlink()