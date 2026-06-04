import os
from dotenv import load_dotenv
from config import API_HOST, API_PORT, ENV_PATH

# 加载 .env 并初始化 LangSmith（必须在任何 LangChain 导入之前）
load_dotenv(ENV_PATH, override=True)
if os.getenv("LANGSMITH_TRACING_V2") == "true" and os.getenv("LANGSMITH_API_KEY"):
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_API_KEY"] = os.getenv("LANGSMITH_API_KEY", "")
    os.environ["LANGSMITH_PROJECT"] = os.getenv("LANGSMITH_PROJECT", "EMA_AI_agent")
    print("🔍 LangSmith 跟踪已启用 -> project:", os.environ["LANGSMITH_PROJECT"])
else:
    print("ℹ️  LangSmith 未配置（设置 LANGSMITH_TRACING_V2=true 和 LANGSMITH_API_KEY 以启用）")


if __name__ == "__main__":
    print(f"🚀 服务器启动中... 地址：http://{API_HOST}:{API_PORT}")

    # 导入以注册所有路由和处理器
    from .trigger import app
    app.start(host=API_HOST, port=API_PORT)