import os
import sys
import nest_asyncio
from logs import init_logger
from dotenv import load_dotenv
from config import STATIC_DIR, SRC_DIR
from config import API_HOST, API_PORT, ENV_PATH

# Fix UnicodeEncodeError for emoji in Windows GBK terminal
if sys.stdout.encoding and sys.stdout.encoding.lower() in ("gbk", "gb2312", "gb18030"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Fix nested event loop conflicts
nest_asyncio.apply()

# Initialize logging
init_logger()

# Load .env and init LangSmith (must be before any LangChain imports)
load_dotenv(ENV_PATH, override=True)
if os.getenv("LANGSMITH_TRACING_V2") == "true" and os.getenv("LANGSMITH_API_KEY"):
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_API_KEY"] = os.getenv("LANGSMITH_API_KEY", "")
    os.environ["LANGSMITH_PROJECT"] = os.getenv("LANGSMITH_PROJECT", "EMA_AI_agent")
    print("🔍 LangSmith tracing enabled -> project:", os.environ["LANGSMITH_PROJECT"])
else:
    print(
        "ℹ️  LangSmith not configured (set LANGSMITH_TRACING_V2=true and LANGSMITH_API_KEY to enable)"
    )


if __name__ == "__main__":
    # Explicit agent-core initialization: skills snapshot + memory store +
    # main tools. Moved out of agent.core import time so tests/tooling can
    # import agent.core without disk I/O. Must run
    # before serving requests and before any lazy agent.core consumer.
    from agent.core import init as init_agent_core

    init_agent_core()

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
