import logging
from models import simple_chat_model

logger = logging.getLogger(__name__)

_EVALUATE_SYSTEM_PROMPT = (
    "You are a notification gate for a background agent. "
    "You will be given the original task and the agent's response. "
    "Call the evaluate_notification tool to decide whether the user "
    "should be notified.\n\n"
    "Notify when the response contains actionable information, errors, "
    "completed deliverables, or anything the user explicitly asked to "
    "be reminded about.\n\n"
    "Suppress when the response is a routine status check with nothing "
    "new, a confirmation that everything is normal, or essentially empty."
)

_EVALUATE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "evaluate_notification",
            "description": "Decide whether the user should be notified about this background task result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "should_notify": {
                        "type": "boolean",
                        "description": "true = result contains actionable/important info the user should see; false = routine or empty, safe to suppress",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One-sentence reason for the decision",
                    },
                },
                "required": ["should_notify"],
            },
        },
    }
]

def evaluate_response(response: str, task_context: str)-> bool:
    """Decide whether a background-task result should be delivered to the user.

    Uses a lightweight tool-call LLM request (same pattern as heartbeat
    ``_decide()``).  Falls back to ``True`` (notify) on any failure so
    that important messages are never silently dropped.
    """
    try:
        llm_response = simple_chat_model.bind_tools(_EVALUATE_TOOL).invoke([
            {"role": "system", "content": _EVALUATE_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"## Original task\n{task_context}\n\n"
                f"## Agent response\n{response}"
            )},
        ])

        if not llm_response.tool_calls or len(llm_response.tool_calls) == 0:
            logger.warning("evaluate_response: no tool call returned, defaulting to notify")
            return True

        args = llm_response.tool_calls[0].get("args", {})

        should_notify = args.get("should_notify", True)
        reason = args.get("reason", "")
        logger.info("evaluate_response: should_notify={}, reason={}", should_notify, reason)

        return bool(should_notify)

    except Exception:
        logger.exception("evaluate_response failed, defaulting to notify")
        return True