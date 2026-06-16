import json
from config import WORKSPACE_DIR
from workspace import ALL_SYSTEM_FILE_NAMES

def read_system_prompt_file()-> dict[str, str]:
    """Read system prompt files"""
    file_to_content: dict[str, str] = {}

    for file_name in ALL_SYSTEM_FILE_NAMES:
        file_path = WORKSPACE_DIR / file_name
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as file:
                file_to_content[file_name] = file.read()

    return file_to_content

def write_system_prompt_file(file_to_content: dict[str, str])->None:
    """Write system prompt files"""
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

def update_system_prompt_file(file_to_content: dict[str, str])->None:
    """Update system prompt files (only overwrite provided files, leave others unchanged)"""
    existing = read_system_prompt_file()

    for file_name, content in file_to_content.items():
        if file_name not in ALL_SYSTEM_FILE_NAMES:
            raise ValueError(f"Invalid file name: {file_name}")
        elif not isinstance(content, str):
            raise ValueError(f"Invalid content type for file: {file_name}")
        elif len(content.strip()) == 0:
            raise ValueError(f"Content is empty for file: {file_name}")
        elif len(content) > 2_000:
            raise ValueError(f"Content too long for file: {file_name}")

        existing[file_name] = content

    write_system_prompt_file(existing)


def read_character()-> dict[str, dict[str, str]]:
    """Read character configuration"""
    file_path = WORKSPACE_DIR / "character.json"
    character_data:dict[str, dict[str, str]] = json.loads(file_path.read_text())
    return character_data

def write_character(character_data: dict[str, dict[str, str]])->None:
    """Write character configuration"""
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

def update_character(character_data: dict[str, dict[str, str]])->None:
    """Update character configuration (only overwrite provided fields, leave others unchanged)"""
    existing = read_character()

    for role_key in ("user", "assistant"):
        incoming_role = character_data.get(role_key)
        if incoming_role is not None:
            if not isinstance(incoming_role, dict):
                raise ValueError(f"Invalid data type for role: {role_key}")
            existing_role = existing.setdefault(role_key, {})
            for field_key in ("name", "avatar"):
                if field_key in incoming_role:
                    value = incoming_role[field_key]
                    if not isinstance(value, str) or len(value.strip()) == 0:
                        raise ValueError(f"Invalid {role_key}.{field_key}: must be non-empty string")
                    existing_role[field_key] = value

    write_character(existing)