"""Prompt-injection adapters for local GGUF chat models.

Local Llama models lack native tool-calling / JSON-schema support, so
:class:`LocalToolBinder` and :class:`LocalStructuredOutput` wrap a
:class:`LocalLlamaChatBase` and translate ``bind_tools`` /
``with_structured_output`` into prompt instructions parsed back into
``AIMessage.tool_calls`` / instructor payloads.
"""

import json
import uuid
import instructor
from typing import Any
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableLambda

from models.LLMs.base_local_llama import LocalLlamaChatBase


class LocalToolBinder:
    """Wraps a model with tool binding via prompt injection."""

    def __init__(self, model: LocalLlamaChatBase):
        self._model = model

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        """Bind tools by injecting a tool-calling system prompt.

        For local GGUF models that lack native tool-call support, this
        instructs the model to respond with a JSON object containing the
        tool name and arguments, then wraps ``_generate`` to parse the
        tool call into the standard ``AIMessage.tool_calls`` format.
        """
        # ── Build tool schemas into a descriptive prompt ────────────
        tool_descriptions = []
        for t in tools:
            if isinstance(t, dict):
                name = t.get("name") or t.get("function", {}).get("name", "unknown")
                desc = t.get("description") or t.get("function", {}).get("description", "")
                params = t.get("parameters") or t.get("function", {}).get("parameters", {})
            elif hasattr(t, "model_fields"):
                # Pydantic model
                name = getattr(t, "__name__", str(t))
                desc = getattr(t, "__doc__", "")
                params = {}
                for fname, field in t.model_fields.items():
                    params[fname] = {
                        "type": str(
                            field.annotation.__name__
                            if hasattr(field.annotation, "__name__")
                            else field.annotation
                        ),
                        "description": (field.description or ""),
                    }
            elif hasattr(t, "name"):
                # BaseTool / @tool-decorated function
                name = t.name
                desc = getattr(t, "description", "")
                args_schema = getattr(t, "args_schema", None)
                if args_schema and hasattr(args_schema, "model_fields"):
                    params = {}
                    for fname, field in args_schema.model_fields.items():
                        params[fname] = {
                            "type": str(
                                field.annotation.__name__
                                if hasattr(field.annotation, "__name__")
                                else field.annotation
                            ),
                            "description": (field.description or ""),
                        }
                else:
                    params = getattr(t, "args", {})
            else:
                continue
            tool_descriptions.append(
                {
                    "name": name,
                    "description": desc,
                    "parameters": params,
                }
            )

        tool_prompt = (
            "You have access to the following tools. When you need to use a tool, "
            "respond with ONLY a valid JSON object in this exact format:\n"
            '{"name": "<tool_name>", "arguments": {<tool_args>}}\n\n'
            "Do NOT include any other text before or after the JSON object.\n\n"
            "Available tools:\n"
        )
        for td in tool_descriptions:
            tool_prompt += f"\n### {td['name']}\n{td['description']}\n"
            if td["parameters"]:
                tool_prompt += (
                    f"Parameters: {json.dumps(td['parameters'], ensure_ascii=False, default=str)}\n"
                )

        if tool_choice and tool_choice != "any":
            tool_prompt += f"\nYou MUST use the tool '{tool_choice}'. Do not use any other tool.\n"

        model = self._model

        def _invoke_with_tools(input_data: Any) -> AIMessage:
            if isinstance(input_data, str):
                msgs: list[BaseMessage] = [
                    HumanMessage(content=input_data),
                ]
            elif isinstance(input_data, list):
                msgs = list(input_data)
            else:
                msgs = [HumanMessage(content=str(input_data))]

            # Inject tool instructions as a system message (prepend)
            has_system = any(isinstance(m, SystemMessage) for m in msgs)
            if has_system:
                for i, m in enumerate(msgs):
                    if isinstance(m, SystemMessage):
                        msgs[i] = SystemMessage(
                            content=m.content + "\n\n" + tool_prompt if m.content else tool_prompt
                        )
                        break
            else:
                msgs.insert(0, SystemMessage(content=tool_prompt))

            result = model._generate(msgs)
            source_msg = result.generations[0].message
            content = source_msg.content or ""
            source_reasoning = source_msg.additional_kwargs.get("reasoning_content")

            # ── Parse tool call from response ──
            parsed_tool_calls = []
            cleaned = content.strip()
            # Try to extract JSON from the response (handle code fences)
            if cleaned.startswith("```"):
                for line in cleaned.split("\n"):
                    if line.strip().startswith("{"):
                        cleaned = line.strip()
                        break
                else:
                    cleaned = cleaned.strip("`").strip()

            if cleaned.startswith("{"):
                try:
                    obj = json.loads(cleaned)
                    name = obj.get("name", "")
                    args = obj.get("arguments", obj.get("args", {}))
                    if name:
                        parsed_tool_calls.append(
                            {
                                "name": name,
                                "args": args if isinstance(args, dict) else {},
                                "id": f"call_{uuid.uuid4().hex[:12]}",
                                "type": "tool_call",
                            }
                        )
                except json.JSONDecodeError:  # noqa: S110
                    pass

            tool_kwargs: dict[str, Any] = {}
            if isinstance(source_reasoning, str) and source_reasoning:
                tool_kwargs.setdefault("reasoning_content", source_reasoning)
            return AIMessage(
                content=content,
                tool_calls=parsed_tool_calls if parsed_tool_calls else None,
                additional_kwargs=tool_kwargs,
            )

        return RunnableLambda(_invoke_with_tools)


class LocalStructuredOutput:
    """Wraps a model with instructor for structured output."""

    def __init__(self, model: LocalLlamaChatBase):
        self._model = model

    def with_structured_output(
        self,
        schema: type | dict[str, Any],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable:
        """Implement structured output via prompt-based JSON generation.

        For local GGUF models that don't support native tool calling,
        this injects a JSON format instruction into the system prompt
        and parses the response with ``PydanticOutputParser``.
        """
        _ = kwargs.pop("method", None)
        _ = kwargs.pop("strict", None)
        if kwargs:
            msg = f"Received unsupported arguments {kwargs}"
            raise ValueError(msg)

        model = self._model

        def _invoke_with_structured(
            input_data: Any,
        ) -> Any:
            if isinstance(input_data, str):
                msgs: list[BaseMessage] = [
                    HumanMessage(content=input_data),
                ]
            elif isinstance(input_data, list):
                msgs = list(input_data)
            else:
                msgs = [HumanMessage(content=str(input_data))]

            try:
                client = model._ensure_client()
                create = instructor.patch(
                    create=client.create_chat_completion_openai_v1,
                    mode=instructor.Mode.JSON,
                )

                return create(
                    messages=[model._convert_message_to_dict(m) for m in msgs],
                    response_model=schema,
                )
            finally:
                model._release_client()

        return RunnableLambda(_invoke_with_structured)
