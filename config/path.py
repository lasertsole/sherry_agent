from pathlib import Path

ROOT_DIR = Path(__file__).parent
ROOT_DIR = ROOT_DIR / ".."
ROOT_DIR = ROOT_DIR.resolve()

ENV_PATH = ROOT_DIR / ".env"

INTERPRETER_PATH = ROOT_DIR / ".venv/Scripts/python"
CONTEXT_ENGINE_PATH = ROOT_DIR / "context_engine"
PLUGINS_PATH = ROOT_DIR / "plugins"

SRC_DIR = ROOT_DIR / "src"
STATIC_DIR = ROOT_DIR / "static"
TEMP_DIR = ROOT_DIR / "temp"

MODELS_DIR = ROOT_DIR / "models"
SESSIONS_DIR = ROOT_DIR / "sessions"
SKILLS_DIR = ROOT_DIR / "skills"
AUTO_SKILLS_DIR = SKILLS_DIR / "auto/"
WORKSPACE_DIR = ROOT_DIR / "workspace"
WORKSPACE_TEMPLATE_DIR = WORKSPACE_DIR / "template"
KNOWLEDGE_DIR = WORKSPACE_DIR / "knowledge"
MEMORY_DIR = WORKSPACE_DIR / "memory"
HEARTBEAT_PATH = WORKSPACE_DIR / "HEARTBEAT.md"

# Additional directories
MEMORY_INDEX_DIR = MEMORY_DIR / "index"
KNOWLEDGE_INDEX_DIR = KNOWLEDGE_DIR / "index"