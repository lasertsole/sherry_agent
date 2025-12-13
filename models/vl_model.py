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
api_key = os.getenv("VL_API_KEY")
api_name = os.getenv("VL_API_NAME")
api_base = os.getenv("VL_API_BASE")
model_provider = os.getenv("VL_MODEL_PROVIDER")

model_config:dict[str, Any] = {
    "model_provider": model_provider,
    "model": api_name,
    "api_key": api_key,
    "base_url": api_base,
    "temperature": 0.8,
    "max_retries": 2
}
VL_API_BASE = "https://api.modelarts-maas.com/v1/chat/completions"
model_config = {k: v for k, v in model_config.items() if v is not None and v != ""}
vl_model = init_chat_model(**model_config) #生成模型对象
vl_model = vl_model.configurable_fields(
    temperature=ConfigurableField(
        id="temperature",
    )
)