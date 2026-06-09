from typing import Literal
from pydantic import BaseModel, Field


class SubAgentOutput(BaseModel):
    """Output of a subagent task."""
    status: Literal["ok", "failed"] = Field(description="Whether the task was completed successfully or not (crash errors).", default="ok")
    finish_reason: str = Field(description="The reason why the task was finish, If the task failed due to tool errors, "
   "permission issues, or content policy violations, please explain the reasons in detail.", default="task completed")
    result: str = Field(description="record the result or result storage path", default="")


class ProgramExecutionResult(BaseModel):
    """Output of program_runner execution."""
    status: Literal["completed", "failed", "interrupted", "timeout", "error"] = Field(default="completed")
    strategy_needed: Literal["fast_retry", "gentle_retry", "full_reset", None] = Field(default=None)
    failed_tasks: list[dict] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)
    can_resume: bool = Field(default=False)
    recommendation: str = Field(default="")
    stage: str = Field(default="unknown")


class RecoveryResult(BaseModel):
    """Output of program_resumer recovery."""
    status: Literal["resumed", "retrying", "reset", "error", "no_failed_tasks"] = Field(default="error")
    strategy: str = Field(default="")
    failed_tasks: list[str] = Field(default_factory=list)
    message: str = Field(default="")
    can_resume: bool = Field(default=True)


class TaskExecutionStatus(BaseModel):
    """Status of individual task execution."""
    label: str
    status: Literal["ok", "failed", "cancelled", "timeout"]
    error: str | None = None
    result: str | None = None
    from_cache: bool = False