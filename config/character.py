import json
from config import WORKSPACE_DIR

_file_path = WORKSPACE_DIR / "character.json"
_character_data: dict[str, dict[str, str]] | None = json.loads(_file_path.read_text(encoding="utf-8"))

if _character_data is None:
    raise ValueError("Invalid character data")

USER_NAME = _character_data.get("user", {}).get("name", "")
ASSISTANT_NAME = _character_data.get("assistant", {}).get("name", "")

if len(USER_NAME.strip()) == 0  or len(ASSISTANT_NAME.strip()) == 0:
    raise ValueError("Invalid character data")