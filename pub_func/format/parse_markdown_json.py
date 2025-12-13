import re
import json
from pydantic import BaseModel

def parse_markdown_json(content: str, model_class: type[BaseModel]) -> BaseModel:
    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
        return model_class(**json.loads(json_str))
    return model_class(**json.loads(content))