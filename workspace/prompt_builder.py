"""System prompt assembly."""

from pathlib import Path
from skills import get_skills_text
from config import WORKSPACE_DIR, TEMP_DIR
from workspace import CORE_SYSTEM_FILE_NAMES, ALL_SYSTEM_FILE_NAMES

MAX_FILE_CHARS: int = 20_000


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if len(text) > MAX_FILE_CHARS:
        return text[:MAX_FILE_CHARS] + "\n...[truncated]"
    return text


skill_guide_text: str = f"""
补充说明：
1.将<skill_folder>替换成技能文件SKILL.md所在的目录 比如技能文件在 "./skills/text_to_image/SKILL.md", 那么文件目录就在 "./skills/text_to_image"
2.技能生成的临时资源（如图片、语音等）存放在{TEMP_DIR.as_posix()}目录下
"""

def build_system_prompt(selected_file_names: list[str] | None = None, selected_skill_names: list[str] | None = None) -> str:
    skill_paths:str = get_skills_text(selected_skill_names)
    skill_paths = f"{skill_paths}\n\n{skill_guide_text}"

    if selected_file_names is not None:
        file_paths = [_read_text(WORKSPACE_DIR / f) for f in selected_file_names]

        # 确保一定有核心文件
        for core_file in CORE_SYSTEM_FILE_NAMES:
            if core_file not in selected_file_names:
                file_paths.append(_read_text(WORKSPACE_DIR / core_file))

    else:
        from tools import memory_store

        file_paths = [
            *[_read_text(WORKSPACE_DIR / f) for f in ALL_SYSTEM_FILE_NAMES],
            memory_store.format_for_system_prompt("memory"),
            memory_store.format_for_system_prompt("user")
        ]

    parts = [*file_paths, skill_paths]

    return "\n\n".join(p for p in parts if p)

