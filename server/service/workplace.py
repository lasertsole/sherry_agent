import json
from config import WORKSPACE_DIR
from workspace import ALL_SYSTEM_FILE_NAMES

def read_system_prompt_file()-> dict[str, str]:
    """读取系统提示文件"""
    file_to_content: dict[str, str] = {}

    for file_name in ALL_SYSTEM_FILE_NAMES:
        file_path = WORKSPACE_DIR / file_name
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as file:
                file_to_content[file_name] = file.read()

    return file_to_content

def write_system_prompt_file(file_to_content: dict[str, str])->None:
    """写入系统提示文件"""
    for file_name, content in file_to_content.items():
        if file_name not in ALL_SYSTEM_FILE_NAMES:
            raise ValueError(f"Invalid file name: {file_name}")
        elif not isinstance(content, str):
            raise ValueError(f"Invalid content type for file: {file_name}")
        elif len(content.strip()) == 0:
            raise ValueError(f"Content is empty for file: {file_name}")
        elif len(content) > 2_000:
            raise ValueError(f"Content too long for file: {file_name}")

        file_path = WORKSPACE_DIR / file_name
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)

def read_character()-> dict[str, dict[str, str]]:
    """读取角色信息"""
    file_path = WORKSPACE_DIR / "character.json"
    character_data:dict[str, dict[str, str]] = json.loads(file_path.read_text())
    return character_data

def write_character(character_data: dict[str, dict[str, str]])->None:
    """写入角色信息"""
    user_dict: dict[str, str] | None = character_data.get("user", None)
    assistant_dict: dict[str, str] | None = character_data.get("assistant", None)

    if (
        not isinstance(user_dict, dict)
        or len(user_dict.get("name", "").strip()) == 0
        or not isinstance(assistant_dict, dict)
        or len(assistant_dict.get("name", "").strip()) == 0
    ):
        raise ValueError("Invalid character data")

    file_path = WORKSPACE_DIR / "character.json"
    file_path.write_text(json.dumps(character_data, indent=4, ensure_ascii=False))