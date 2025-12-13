from typing import Literal
from pydantic import BaseModel, Field

class SubAgentOutput(BaseModel):
    """Output of a subagent task."""
    status: Literal["ok", "failed"] = Field(description="Whether the task was completed successfully or not (crash errors).", default="ok")
    finish_reason: str = Field(description="The reason why the task was finish, If the task failed due to tool errors, "
   "permission issues, or content policy violations, please explain the reasons in detail.", default="task completed")
    result: str = Field(description="record the result or result storage path", default="")