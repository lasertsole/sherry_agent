from .path import (
    ROOT_DIR as ROOT_DIR,
    ENV_PATH as ENV_PATH,
    INTERPRETER_PATH as INTERPRETER_PATH,
    CONTEXT_ENGINE_PATH as CONTEXT_ENGINE_PATH,
    PLUGINS_PATH as PLUGINS_PATH,
    SRC_DIR as SRC_DIR,
    STATIC_DIR as STATIC_DIR,
    TEMP_DIR as TEMP_DIR,
    MODELS_DIR as MODELS_DIR,
    SESSIONS_DIR as SESSIONS_DIR,
    SKILLS_DIR as SKILLS_DIR,
    AUTO_SKILLS_DIR as AUTO_SKILLS_DIR,
    PLUGIN_SKILLS_DIR as PLUGIN_SKILLS_DIR,
    SKILLS_STATE_FILE as SKILLS_STATE_FILE,
    SKILL_DISCOVERY_ROOTS as SKILL_DISCOVERY_ROOTS,
    is_allowed_skill_path as is_allowed_skill_path,
    WORKSPACE_DIR as WORKSPACE_DIR,
    WORKSPACE_TEMPLATE_DIR as WORKSPACE_TEMPLATE_DIR,
    KNOWLEDGE_DIR as KNOWLEDGE_DIR,
    MEMORY_DIR as MEMORY_DIR,
    HEARTBEAT_PATH as HEARTBEAT_PATH,
    HEARTBEAT_TEMPLATE_PATH as HEARTBEAT_TEMPLATE_PATH,
    MEMORY_INDEX_DIR as MEMORY_INDEX_DIR,
    KNOWLEDGE_INDEX_DIR as KNOWLEDGE_INDEX_DIR,
    WORKSPACE_TEMPLATE_LANGS as WORKSPACE_TEMPLATE_LANGS,
    DEFAULT_WORKSPACE_TEMPLATE_LANG as DEFAULT_WORKSPACE_TEMPLATE_LANG,
    resolve_workspace_template_lang as resolve_workspace_template_lang,
    resolve_workspace_template_dir as resolve_workspace_template_dir,
)
from .features import GATEWAY as GATEWAY
from .num import (
    ARCHIVE_THRESHOLD as ARCHIVE_THRESHOLD,
    MEMORY_THRESHOLD as MEMORY_THRESHOLD,
    COMPRESS_RATIO as COMPRESS_RATIO,
)

# Bind address for the Robyn backend. Both are sourced from the gateway
# registry, which reads API_HOST/API_PORT from the environment at import so
# the server can bind 0.0.0.0 inside containers (Dockerfile sets API_HOST);
# defaults keep loopback for local development.
API_HOST: str = GATEWAY["api_host"]
API_PORT: int = GATEWAY["api_port"]
