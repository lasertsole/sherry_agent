import os
from typing import Any
from pathlib import Path
from config import ENV_PATH
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.runnables import ConfigurableField

# 获取当前所在文件夹
current_dir = Path(__file__).parent.resolve()

# 加载环境变量
load_dotenv(ENV_PATH, override = True)
api_key = os.getenv("AUXILIARY_LLM_API_KEY")
api_name = os.getenv("AUXILIARY_LLM_API_NAME")
model_provider = os.getenv("AUXILIARY_LLM_PROVIDER")
api_base = os.getenv("AUXILIARY_LLM_API_BASE")
max_tokens = os.getenv("AUXILIARY_LLM_MAX_TOKEN")
if max_tokens:
    max_tokens = min(int(max_tokens), 65536)

model_config:dict[str, Any] = {
    "model_provider": model_provider,
    "model": api_name,
    "api_key": api_key,
    "base_url": api_base,
    "temperature": 0,
    "max_retries": 2,
    "profile": {"max_input_tokens": max_tokens}  # 设置模型上下文窗口大小
}
model_config = {k: v for k, v in model_config.items() if v is not None and v != ""}
auxiliary_llm = init_chat_model(**model_config) #生成模型对象
auxiliary_llm = auxiliary_llm.configurable_fields(
    temperature=ConfigurableField(
        id="temperature",
    )
)