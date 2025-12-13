import os
import json
from config import SKILLS_DIR

def build_skills_snapshot() -> None:
    from .loader import scan_skills

    skills: list[dict[str, str]] = scan_skills()
    skills_json :str = json.dumps(skills, ensure_ascii=False, indent=4)
    with open(os.path.join(SKILLS_DIR, 'skills_snapshot.json'), 'w', encoding='utf-8') as f:
        f.write(skills_json)

def read_skills_snapshot() -> list[dict[str, str]] | None:
    file_path:str = os.path.join(SKILLS_DIR, 'skills_snapshot.json')

    if os.path.exists(file_path):
        with open(os.path.join(SKILLS_DIR, file_path), 'r', encoding='utf-8') as f:
            skills_json:str = f.read()
            skills: list[dict[str, str]] = json.loads(skills_json)
            return skills

    return None