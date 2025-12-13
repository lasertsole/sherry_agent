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
api_key = os.getenv("REASONER_CHAT_API_KEY")
api_name = os.getenv("REASONER_CHAT_API_NAME")
model_provider = os.getenv("REASONER_CHAT_MODEL_PROVIDER")

model_config:dict[str, Any] = {
    "model_provider": model_provider,
    "model": api_name,
    "api_key": api_key,
    "temperature": 0.5,
    "max_retries": 2
}
model_config = {k: v for k, v in model_config.items() if v is not None and v != ""}
#推理模型
reasoner_model = init_chat_model(**model_config)
reasoner_model = reasoner_model.configurable_fields(
    temperature=ConfigurableField(
        id="temperature",
    )
)