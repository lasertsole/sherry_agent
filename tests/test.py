from pydantic import BaseModel
import instructor
from models import auxiliary_llm

class RES(BaseModel):
    capital: str

# instructor.from_openai(
#     auxiliary_llm,
#     instructor.Mode.JSON_SCHEMA
# )

res = auxiliary_llm.with_structured_output(RES).invoke("What is the capital of France?")
print(res)