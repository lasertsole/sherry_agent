import os
import sys
import nest_asyncio
from logs import init_logger
from dotenv import load_dotenv
from config import STATIC_DIR, SRC_DIR
from config import API_HOST, API_PORT, ENV_PATH
from config.sherry_settings import get_sherry_setting

# Fix UnicodeEncodeError for emoji in Windows GBK terminal
for _stream in (sys.stdout, sys.stderr):
    _encoding = str(getattr(_stream, "encoding", "") or "").lower()
    if _encoding in ("gbk", "gb2312", "gb18030"):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if callable(_reconfigure):
            _reconfigure(encoding="utf-8", errors="replace")

# Fix nested event loop conflicts
nest_asyncio.apply()

# Initialize logging
init_logger()

# Load .env and init LangSmith (must be before any LangChain imports).
# LangSmith settings live in sherry.jsonc (see config/sherry_settings.py).
load_dotenv(ENV_PATH, override=True)
if bool(get_sherry_setting("LANGSMITH.TRACING_V2")) and str(
    get_sherry_setting("LANGSMITH.API_KEY")
):
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_API_KEY"] = str(get_sherry_setting("LANGSMITH.API_KEY"))
    os.environ["LANGSMITH_PROJECT"] = str(get_sherry_setting("LANGSMITH.PROJECT"))
    print("🔍 LangSmith tracing enabled -> project:", os.environ["LANGSMITH_PROJECT"])
else:
    print(
        "ℹ️  LangSmith not configured (set LANGSMITH.TRACING_V2=true and LANGSMITH.API_KEY in sherry.jsonc to enable)"
    )


if __name__ == "__main__":
    # Provider registry injection: config.schema must not import models, so the
    # registry (self-registering on import) is pulled in explicitly here as a
    # boot guarantee. Idempotent — import of models.providers already registers.
    from models.providers.registry import register_provider_registry

    register_provider_registry()

    # Crash-loop gate (runtime.process.crash_loop_breaker): record this boot before
    # any background service is allowed to start. The clean-exit marker is
    # ONE-SHOT, so it must be read BEFORE record_boot consumes it.
    import atexit

    from loguru import logger

    import runtime.process.crash_loop_breaker as breaker
    from runtime.process.crash_loop_breaker import mark_clean_exit, was_last_exit_clean

    _prev_exit_clean = was_last_exit_clean()
    tripped = breaker.record_boot(clean=_prev_exit_clean, reason="startup")
    # Clean-exit self-heal: a graceful shutdown marks the next boot clean, so
    # normal restarts never accumulate unclean boots. No auto-reset otherwise.
    atexit.register(mark_clean_exit)

    if tripped:
        # Crash loop (3+ unclean boots in 5 min): enter HTTP-only mode.
        # The curator and cron imports below are skipped entirely (their
        # background threads are the crash loop's fuel); the trigger import
        # is kept so HTTP/WS routes stay available for inspection and
        # manual intervention.
        os.environ["SHERRY_HTTP_ONLY"] = "1"
        logger.critical(
            "CrashLoopBreaker TRIPPED (3+ unclean boots within 5 min): entering "
            "HTTP-only mode -- curator and cron background services are DISABLED, "
            "HTTP/WS still served. Self-heal: exit cleanly once (atexit marker "
            "clears the trip on next boot). Manual reset: delete {}",
            SRC_DIR / "data" / "boot_lifecycle.json",
        )

    # MAX_TOKEN gate (config.features.token_guard): both agents must run on a
    # >= 128K context window. Fail fast BEFORE any agent-core work, so a
    # misconfigured .env can never serve a crippled agent.
    from config.features import LLM_CLIENT_DEFAULTS, assert_max_token_valid

    _main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
    _aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()

    # Resolve effective values (mirrors main_llm.py / auxiliary_llm/core.py logic):
    # an unset AUXILIARY_LLM_MAX_TOKEN falls back to
    # LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072 (< 128K), so leaving
    # it unset blocks startup too. Local mode (AUXILIARY_LLM_MODEL_LOCAL=true)
    # is NOT exempt.
    _main_val = int(_main_raw) if _main_raw else None
    _aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]

    try:
        assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
        assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
    except Exception as e:
        logger.critical("STARTUP ABORTED: {}", e)
        raise SystemExit(1) from e

    # Explicit agent-core initialization: skills snapshot + memory store +
    # main tools. Moved out of agent.core import time so tests/tooling can
    # import agent.core without disk I/O. Must run
    # before serving requests and before any lazy agent.core consumer.
    # Runs unconditionally (also in HTTP-only mode): HTTP requests still
    # need the agent core.
    from agent.core import init as init_agent_core

    init_agent_core()

    if not tripped:
        # run curator to maintain auto-skills — starts the curator background
        # thread (moved out of context_engine.curator import time)
        from context_engine.curator import init as init_curator

        init_curator()

        # run core service thread — starts the cron-service background thread
        # (moved out of cron.scripts.base import time)
        from skills.builtin.core.cron.scripts import init as init_cron

        init_cron()

    # Register all HTTP/WS/channel/subagent routes and handlers explicitly
    # (moved out of server.trigger import time)
    from .trigger import app, init as init_trigger

    init_trigger()

    # Lane lifecycle: fail-fast config validation + drain gate, before app.start().
    from server.service.lane_lifecycle import install_lane_lifecycle

    install_lane_lifecycle()

    # Pin robyn to a single worker process. `--fast` sets processes=(cpu*2)+1,
    # spawning a process pool; every in-memory registry (relation_register,
    # state registers, input queues, subagent registry) lives per-process, so
    # a WS connection registered in one worker is invisible to a turn running
    # in another — chunk/done frames get silently dropped (websocket=None).
    app.config.processes = 1
    app.config.workers = 1

    # Configuring Static File Directory Hosting
    app.serve_directory(
        route="/static",  # URL prefix accessed by the client.
        directory_path=os.path.join(os.getcwd(), STATIC_DIR.absolute().as_posix()),
    )

    # Ensure the /images upload directory exists before serving it statically.
    images_dir = SRC_DIR / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    app.serve_directory(route="/images", directory_path=os.path.join(os.getcwd(), str(images_dir)))

    # Ensure the /audio and /video upload directories exist before serving them statically.
    audio_dir = SRC_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    app.serve_directory(route="/audio", directory_path=os.path.join(os.getcwd(), str(audio_dir)))

    video_dir = SRC_DIR / "video"
    video_dir.mkdir(parents=True, exist_ok=True)

    app.serve_directory(route="/video", directory_path=os.path.join(os.getcwd(), str(video_dir)))

    app.start(host=API_HOST, port=API_PORT)
