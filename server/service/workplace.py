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