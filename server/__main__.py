import os
import nest_asyncio
from logs import init_logger
from dotenv import load_dotenv
from config import API_HOST, API_PORT, ENV_PATH

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
    print("ℹ️  LangSmith not configured (set LANGSMITH_TRACING_V2=true and LANGSMITH_API_KEY to enable)")


if __name__ == "__main__":
    # Import triggers to register all routes and handlers
    from .trigger import app
    app.start(host=API_HOST, port=API_PORT)