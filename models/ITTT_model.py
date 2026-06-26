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
api_key = os.getenv("ITTT_API_KEY")
api_name = os.getenv("ITTT_API_NAME")
api_base = os.getenv("ITTT_API_BASE")
model_provider = os.getenv("ITTT_MODEL_PROVIDER")

model_config:dict[str, Any] = {
    "model_provider": model_provider,
    "model": api_name,
    "api_key": api_key,
    "base_url": api_base,
    "temperature": 0.8,
    "max_retries": 2
}

model_config = {k: v for k, v in model_config.items() if v is not None and v != ""}
ITTT_model = init_chat_model(**model_config) #生成模型对象
ITTT_model = ITTT_model.configurable_fields(
    temperature=ConfigurableField(
        id="temperature",
    )
)