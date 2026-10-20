"""TaskFlow decision LLM (``TASKFLOW_LLM_*``).

Serves the four TaskFlow-internal calls: ``taskflow_plan`` /
``taskflow_replan`` / ``step_judge`` / ``taskflow_finish``. An unconfigured
family falls back to the auxiliary model, so an existing deployment sees no
change. See :mod:`models.LLMs.family_llm` for the three-way rule.
"""

from typing import Any

from models.LLMs.family_llm import build_family_llm

__all__ = ["build_taskflow_llm"]


def build_taskflow_llm(temperature: float | None = None) -> Any:
    """Create a fresh TaskFlow decision model bound to the current event loop."""
    return build_family_llm("TASKFLOW_LLM", temperature)
