import re
import inspect
import functools
from typing import Any, Callable, Sequence, cast
from pydantic import BaseModel, ValidationError
from .utils import extract_and_combine_codeblocks
from langchain_core.messages import AnyMessage, SystemMessage
from langchain_core.tools import tool as create_tool
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import StructuredTool, BaseTool
from langgraph.graph import END, START, StateGraph, MessagesState
from langgraph.types import Command
from langgraph.typing import ContextT
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain.agents.factory import (
    _ComposedExtendedModelResponse,
    _normalize_to_model_response,
    _build_commands,
    _chain_model_call_handlers,
    _add_middleware_edge,
    JumpTo,
)
from langgraph._internal._runnable import RunnableCallable


EvalFunction = Callable[[str, dict[str, Any]], tuple[str, dict[str, Any]]]

# ---------------------------------------------------------------------------
# Helper: build a ModelRequest for codeact
# ---------------------------------------------------------------------------
def _build_model_request(
    model: BaseChatModel,
    tools: list[BaseTool],
    prompt: str,
    state: Any,
    response_format: type[BaseModel] | None = None,
) -> ModelRequest:
    """Build a ModelRequest for the codeact agent.

    CodeAct does not use ToolStrategy / ProviderStrategy, so response_format
    is kept as a raw schema (passed through to the middleware so it *can* be
    overridden, but the codeact layer itself treats it as ``None`` for
    ``get_bound_model`` purposes --- codeact handles structured output
    entirely in its ``structured_output`` node).
    """
    return ModelRequest(
        model=model,
        messages=list(state["messages"]),
        system_message=SystemMessage(content=prompt),
        tools=tools,
        response_format=response_format,  # type: ignore[arg-type]
        state=state,
        runtime=None,
    )


# ---------------------------------------------------------------------------
# CodeActState
# ---------------------------------------------------------------------------
class CodeActState(MessagesState):
    """CodeAct agent 的状态 schema。

    Attributes:
        messages: 对话消息列表（由 MessagesState 提供，带 add_messages reducer）
        script: 当前要执行的 Python 代码（由 LLM 生成）
        context: 执行上下文，包含可用的工具函数和所有之前的变量
        structured_response: 可选的 structured output（由 response_format 控制）
        jump_to: 可选的跳转目标（由 middleware 使用）
    """
    script: str | None
    context: dict[str, Any]
    structured_response: Any | None
    jump_to: str | None


def _tool_signature(tool: BaseTool) -> str:
    """Extract a clean Python function signature for ``tool``.

    Priority:
      1. ``StructuredTool`` with ``tool.func`` → inspect the wrapped function directly.
      2. Any tool with ``args_schema`` (a Pydantic ``BaseModel``) → build the
         signature from model fields (preferred: handles defaults, types).
      3. Fallback to ``(*args, **kwargs)``.

    The returned signature string does **not** include the leading ``(`` / trailing ``)``
    so callers can interpolate it naturally::

        f"def {tool.name}({signature}):"
    """
    # ── Priority 1: StructuredTool with a wrapped function ──
    if isinstance(tool, StructuredTool) and tool.func is not None:
        sig = inspect.signature(tool.func)
        # Strip `self` (unlikely for @tool but present for class-bound fns)
        params = [
            p for p in sig.parameters.values()
            if p.name != "self"
        ]
        # Strip `run_manager` / `kwargs` / `callbacks` — LangChain plumbing
        hidden = {"run_manager", "kwargs", "callbacks", "tool_call_id"}
        params = [p for p in params if p.name not in hidden]
        return _format_params(params)

    # ── Priority 2: args_schema (Pydantic model) ──
    schema: BaseModel | None = getattr(tool, "args_schema", None)
    if schema is not None and isinstance(schema, type) and issubclass(schema, BaseModel):
        model_fields = getattr(schema, "model_fields", None)
        if model_fields:
            parts = []
            for fname, field in model_fields.items():
                # Build type annotation
                ann = field.annotation
                ann_str = _type_str(ann) if ann is not None else "Any"
                # Build default
                if field.is_required():
                    parts.append(f"{fname}: {ann_str}")
                else:
                    default = field.default
                    if default is None:
                        parts.append(f"{fname}: {ann_str} = None")
                    elif isinstance(default, str):
                        parts.append(f"{fname}: {ann_str} = {default!r}")
                    elif isinstance(default, (int, float, bool)):
                        parts.append(f"{fname}: {ann_str} = {default}")
                    elif default is ...:
                        parts.append(f"{fname}: {ann_str}")
                    else:
                        parts.append(f"{fname}: {ann_str} = {default}")
            return ", ".join(parts)

    # ── Priority 3: fallback ──
    return "*args, **kwargs"


def _format_params(params: list[inspect.Parameter]) -> str:
    """Format a list of ``inspect.Parameter`` into a clean signature string."""
    parts = []
    for p in params:
        # name: type
        ann = p.annotation
        ann_str = _type_str(ann) if ann is not inspect.Parameter.empty else "Any"
        default_str = ""
        if p.default is not inspect.Parameter.empty:
            if p.default is None:
                default_str = " = None"
            elif isinstance(p.default, str):
                default_str = f" = {p.default!r}"
            elif isinstance(p.default, (int, float, bool)):
                default_str = f" = {p.default}"
            else:
                default_str = f" = {p.default}"
        parts.append(f"{p.name}: {ann_str}{default_str}")
    return ", ".join(parts)


def _type_str(ann: type) -> str:
    """Convert a type annotation to a human-readable string.

    Handles common generics (``Optional[str]``, ``list[int]``, ``str | None``).
    """
    # NoneType -> "None"
    if ann is type(None):
        return "None"
    origin = getattr(ann, "__origin__", None)
    if origin is None:
        try:
            return ann.__name__
        except AttributeError:
            return str(ann)
    # GenericAlias (list[str], dict[str, int], Optional[...], Union[...])
    args = getattr(ann, "__args__", ())
    # Detect Union / Optional (both typing.Union and types.UnionType)
    _union_origin = None
    try:
        import types
        _union_origin = types.UnionType
    except Exception:
        pass
    try:
        _typing_union = getattr(__import__("typing"), "Union", None)
    except Exception:
        _typing_union = None
    _is_union = origin in (_union_origin, _typing_union)

    if _is_union:
        # Render as ``str | None`` rather than ``str, NoneType``
        elem_strs = [_type_str(a) for a in args]
        return " | ".join(elem_strs)

    # Non-union generic containers
    args_str = ", ".join(_type_str(a) for a in args)
    if origin is list:
        return f"list[{args_str}]"
    if origin is dict:
        return f"dict[{args_str}]"
    if origin is tuple:
        return f"tuple[{args_str}]"
    if origin is set:
        return f"set[{args_str}]"
    if origin is type(None):
        # Special-case: NoneType annotation -> None
        return "None"
    return str(ann)


def _response_format_schema_text(schema: type[BaseModel]) -> str:
    """Render a Pydantic model as a JSON Schema text block for the system prompt."""
    try:
        # Pydantic v2
        schema_json = schema.model_json_schema()
    except AttributeError:
        schema_json = schema.schema()
    import json
    return json.dumps(schema_json, indent=2)


def create_default_prompt(
    tools: list[StructuredTool],
    base_prompt: str | None = None,
    *,
    response_format: type[BaseModel] | None = None,
) -> str:
    """为 CodeAct agent 创建默认系统提示词。"""
    tools = [t if isinstance(t, BaseTool) else create_tool(t) for t in tools]
    prompt = f"{base_prompt}\n\n" if base_prompt else ""
    prompt += """You are an agent that controls tools by writing Python code.

## CRITICAL: Output format

You MUST output EITHER:

1. A Python code snippet (in a fenced code block starting with ```python)
   that calls the available functions to accomplish the task step by step.
   Any output you want to extract from the code should be printed to the console.

2. Plain text (NO code block) if you want to ask for more information or provide the final answer.
"""

    if response_format is not None:
        schema_text = _response_format_schema_text(response_format)
        prompt += f"""
3. A JSON code block (```json) containing the final structured response.
   Use this format ONLY when the task is fully complete and you need to return
   structured data matching the schema below.

## Structured Output Schema

When the task is complete, output your structured result inside a JSON code block:

```json
{{"field1": "value1", "field2": "value2"}}
```

The JSON object MUST conform to this schema:

```json
{schema_text}
```

The JSON code block will be parsed and returned as the final structured response.
Do NOT mix Python code blocks with JSON code blocks in the same message.
"""

    prompt += """
## ABSOLUTELY FORBIDDEN

Do NOT output tool calls in JSON format. Do NOT use <tool_call> or any JSON-based function-calling syntax.
You must use ```python code blocks to invoke tools. This is the ONLY valid way to call tools.

## Available functions

In addition to the Python Standard Library, you can use the following functions:
"""

    for tool in tools:
        sig = _tool_signature(tool)
        prompt += f"""
def {tool.name}({sig}):
    \"\"\"{tool.description}\"\"\"
    ...
"""

    prompt += """

Variables defined at the top level of previous code snippets can be referenced in your code.

Reminder: use Python code snippets to call tools"""
    return prompt


def _execute_model_sync(
    request: ModelRequest[ContextT],
    *,
    prompt: str,
    eval_fn: EvalFunction,
    tools_context: dict[str, Any],
    response_format: type[BaseModel] | None,
    name: str | None = None,
) -> ModelResponse:
    """Execute the CodeAct model step.

    This is the core model execution logic wrapped by ``wrap_model_call``
    middleware handlers.
    """
    # Build full messages: system prompt + conversation
    messages: list[AnyMessage] = []
    if request.system_message:
        messages.append(request.system_message)
    messages.extend(request.messages)

    output = request.model.invoke(messages)

    if name:
        output.name = name

    # CodeAct does NOT use ToolStrategy/ProviderStrategy for structured output;
    # it extracts JSON code blocks in the ``structured_output`` graph node.
    # So ``structured_response`` is always ``None`` at this level.
    return ModelResponse(result=[output], structured_response=None)


def create_codeact(
    model: BaseChatModel,
    tools: Sequence[StructuredTool | Callable],
    eval_fn: EvalFunction,
    *,
    system_prompt: str | None = None,
    state_schema: type = CodeActState,
    response_format: type[BaseModel] | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
) -> StateGraph:
    """创建 CodeAct agent。

    CodeAct 架构中，LLM 输出 Python 代码块 → 在沙箱中执行 → 结果返回给 LLM，
    循环直到 LLM 直接回答（无代码块）。

    Args:
        model: 用于生成代码的语言模型
        tools: agent 可用的工具列表（普通函数或 StructuredTool）
        eval_fn: 在沙箱中执行代码的函数。
            接受 (code_string, locals_dict) -> (stdout_output, new_vars_dict)
        system_prompt: 可选的自定义系统提示词（会拼接在默认提示词前）。
            为 None 则完全使用默认提示词。
        state_schema: agent 使用的 state schema。
        response_format: 可选的 Pydantic 模型，提供时 LLM 输出 JSON 代码块
            会解析为结构化响应并存储在 structured_response 中。
        middleware: 可选的 middleware 列表，用于拦截/增强 agent 执行。

    Returns:
        一个编译好的 CodeAct agent（可被直接调用）
    """
    tools = [t if isinstance(t, BaseTool) else create_tool(t) for t in tools]
    middleware = list(middleware) if middleware else []

    prompt = create_default_prompt(tools, base_prompt=system_prompt, response_format=response_format)

    # 使工具函数在代码沙箱中可用
    tools_context = {}
    for tool in tools:
        if isinstance(tool, StructuredTool) and tool.func is not None:
            tools_context[tool.name] = tool.func
        else:
            # Wrap tool.run so the LLM can call it with keyword arguments
            tools_context[tool.name] = functools.partial(tool.run)

    # ------------------------------------------------------------------
    # Build wrap_model_call handler chain from middleware
    # ------------------------------------------------------------------
    wrap_model_call_handlers = [
        m.wrap_model_call for m in middleware
        if hasattr(m, "wrap_model_call") and type(m).wrap_model_call is not AgentMiddleware.wrap_model_call
    ]
    composed_wrap_model_call = _chain_model_call_handlers(wrap_model_call_handlers)

    # ------------------------------------------------------------------
    # collect middleware-provided tools (codeact doesn't use them, but
    # we keep the interface consistent)
    # ------------------------------------------------------------------
    for mw in middleware:
        if hasattr(mw, "tools") and mw.tools:
            pass  # CodeAct does not dynamically add tools

    # ------------------------------------------------------------------
    # merge middleware state schemas into CodeActState
    # ------------------------------------------------------------------
    merged_schema: type = state_schema
    for mw in middleware:
        mw_schema = getattr(mw, "state_schema", None)
        if mw_schema is not None and mw_schema is not type(None):
            # Merge base fields into the schema if not already present
            # For simplicity we keep the declared CodeActState which already
            # has jump_to - middleware can extend via Pydantic annotations.
            pass

    # ------------------------------------------------------------------
    # Internal nodes
    # ------------------------------------------------------------------
    def call_model(state: state_schema) -> Command:
        # Build ModelRequest
        request = _build_model_request(
            model=model,
            tools=tools,
            prompt=prompt,
            state=state,
            response_format=response_format,
        )

        # Define the inner handler that actually executes the model
        def _inner(request: ModelRequest[ContextT]) -> ModelResponse:
            return _execute_model_sync(
                request,
                prompt=prompt,
                eval_fn=eval_fn,
                tools_context=tools_context,
                response_format=response_format,
            )

        # Execute through middleware chain or directly
        if composed_wrap_model_call is not None:
            result = composed_wrap_model_call(request, _inner)
            if isinstance(result, _ComposedExtendedModelResponse):
                model_response = result.model_response
                extra_commands = result.commands
            elif isinstance(result, ExtendedModelResponse):
                model_response = result.model_response
                extra_commands = [result.command] if result.command else []
            else:
                model_response = _normalize_to_model_response(result)
                extra_commands = []

            response = model_response.result[-1] if model_response.result else None
            commands_result = _build_commands(model_response, extra_commands)
            state_updates: list[dict[str, Any]] = [cmd.update for cmd in commands_result if cmd.update]
            merged_update: dict[str, Any] = {}
            for u in state_updates:
                merged_update.update(u)
        else:
            model_response = _inner(request)
            response = model_response.result[-1] if model_response.result else None
            merged_update = {"messages": model_response.result}

        if response is None or not hasattr(response, "content"):
            # Fallback: no response produced
            return Command(update={"script": None})

        content: str = response.content

        # Determine routing destinations (through after_model chain if hooks exist)
        goto_structured = after_model_first_node if hook_after_model else "structured_output"
        goto_sandbox = after_model_first_node if hook_after_model else "sandbox"

        # 检测 JSON 代码块 → 路由到 structured_output 节点
        if response_format is not None and _has_json_codeblock(content):
            return Command(
                goto=goto_structured,
                update={"messages": [response], "script": None, **merged_update},
            )

        # 普通代码块提取 → 沙箱执行
        code = extract_and_combine_codeblocks(content)
        if code:
            return Command(
                goto=goto_sandbox,
                update={"messages": [response], "script": code, **merged_update},
            )
        else:
            # 没有代码块，结束循环并回答用户
            return Command(update={"messages": [response], "script": None, **merged_update})

    def structured_output(state: state_schema) -> Command:
        """解析 LLM 输出的 JSON 代码块并验证结构。"""
        if response_format is None:
            return Command(
                update={
                    "messages": [
                        {"role": "user", "content": "Internal error: structured_output called without response_format."}
                    ],
                    "script": None,
                }
            )
        last_message = state["messages"][-1]
        json_str = _extract_json_codeblock(last_message.content)
        try:
            parsed = response_format.model_validate_json(json_str)
        except ValidationError as e:
            return Command(
                goto="call_model",
                update={
                    "messages": [
                        last_message,
                        {
                            "role": "user",
                            "content": (
                                f"Failed to parse structured response: {e}\n\n"
                                "Please ensure your JSON code block matches the expected schema "
                                "and try again."
                            ),
                        },
                    ],
                    "script": None,
                }
            )
        return Command(
            update={
                "script": None,
                "structured_response": parsed,
            }
        )

    def sandbox(state: state_schema) -> dict:
        existing_context = state.get("context", {})
        context = {**existing_context, **tools_context}
        # 在沙箱中执行脚本
        output, new_vars = eval_fn(state["script"], context)
        new_context = {**existing_context, **new_vars}
        content = output
        return {
            "messages": [{"role": "user", "content": content}],
            "context": new_context,
        }

    # ------------------------------------------------------------------
    # Build graph and inject middleware edges
    # ------------------------------------------------------------------
    agent = StateGraph(state_schema)
    agent.add_node("call_model", call_model, destinations=(END, "sandbox", "structured_output"))
    agent.add_node("sandbox", sandbox)
    agent.add_node("structured_output", structured_output, destinations=(END, "call_model"))

    # Determine if any middleware has before_agent/after_agent hooks
    hook_before_agent: list[Any] = [m for m in middleware if _has_before_agent_hook(m)]
    hook_after_agent: list[Any] = [m for m in middleware if _has_after_agent_hook(m)]
    hook_before_model: list[Any] = [m for m in middleware if _has_before_model_hook(m)]
    hook_after_model: list[Any] = [m for m in middleware if _has_after_model_hook(m)]

    # Resolve routing targets for model hooks (used inside call_model closure)
    after_model_first_node: str | None = (
        f"{hook_after_model[0].name}.after_model" if hook_after_model else None
    )

    # Before/after agent hooks — only inject if at least one middleware provides them
    if hook_before_agent:
        inject_hooks(
            agent,
            middleware_list=hook_before_agent,
            suffix="before_agent",
            hook_attr="before_agent",
            ahook_attr="abefore_agent",
            default_destination="call_model",
            model_destination="call_model",
            end_destination=END,
        )

    if hook_after_agent:
        inject_hooks(
            agent,
            middleware_list=hook_after_agent,
            suffix="after_agent",
            hook_attr="after_agent",
            ahook_attr="aafter_agent",
            default_destination="structured_output",  # after last model call
            model_destination="call_model",
            end_destination=END,
        )

    # Before/after model hooks
    if hook_before_model:
        inject_hooks(
            agent,
            middleware_list=hook_before_model,
            suffix="before_model",
            hook_attr="before_model",
            ahook_attr="abefore_model",
            default_destination="call_model",
            model_destination="call_model",
            end_destination=END,
        )

    if hook_after_model:
        inject_hooks(
            agent,
            middleware_list=hook_after_model,
            suffix="after_model",
            hook_attr="after_model",
            ahook_attr="aafter_model",
            default_destination="sandbox",  # after model, routing to next node
            model_destination="call_model",
            end_destination=END,
        )

    # --- START edge ---
    if hook_before_agent:
        agent.add_edge(START, f"{hook_before_agent[0].name}.before_agent")
        # Last before_agent node already edges to "call_model" via _add_middleware_edge
    elif hook_before_model:
        # Route through before_model chain first (last before_model → call_model via _add_middleware_edge)
        agent.add_edge(START, f"{hook_before_model[0].name}.before_model")
    else:
        agent.add_edge(START, "call_model")

    # --- sandbox → call_model loop edge ---
    # If before_model hooks exist, sandbox routes through them
    if hook_before_model:
        agent.add_edge("sandbox", f"{hook_before_model[0].name}.before_model")
    else:
        agent.add_edge("sandbox", "call_model")

    return agent


# ---------------------------------------------------------------------------
# Utility functions for middleware injection
# ---------------------------------------------------------------------------

def _has_before_agent_hook(mw: AgentMiddleware) -> bool:
    return (hasattr(mw, "before_agent")
            and type(mw).before_agent is not AgentMiddleware.before_agent)


def _has_after_agent_hook(mw: AgentMiddleware) -> bool:
    return (hasattr(mw, "after_agent")
            and type(mw).after_agent is not AgentMiddleware.after_agent)


def _has_before_model_hook(mw: AgentMiddleware) -> bool:
    return (hasattr(mw, "before_model")
            and type(mw).before_model is not AgentMiddleware.before_model)


def _has_after_model_hook(mw: AgentMiddleware) -> bool:
    return (hasattr(mw, "after_model")
            and type(mw).after_model is not AgentMiddleware.after_model)


def inject_hooks(
    agent: StateGraph,
    middleware_list: list[AgentMiddleware],
    suffix: str,
    hook_attr: str,
    ahook_attr: str,
    default_destination: str,
    model_destination: str,
    end_destination: str,
) -> None:
    """Inject middleware hook nodes between other graph nodes.

    Each middleware in the list gets its own graph node named
    ``{middleware.name}.{suffix}``. Nodes are chained in order.
    """
    for i, mw in enumerate(middleware_list):
        node_name = f"{mw.name}.{suffix}"
        sync_fn = getattr(mw, hook_attr, None)
        async_fn = getattr(mw, ahook_attr, None)

        # Determine if this middleware actually defines the hook
        has_sync = sync_fn is not None and type(mw).__dict__.get(hook_attr) is not None
        has_async = async_fn is not None and type(mw).__dict__.get(ahook_attr) is not None

        if not has_sync and not has_async:
            continue

        def make_node(fn_sync, fn_async, node_name=node_name):
            def _run(state):
                result = fn_sync(state, runtime=None) if fn_sync else None
                if result is None:
                    result = {}
                if isinstance(result, dict):
                    return result
                return {}
            return _run

        agent.add_node(node_name, make_node(sync_fn, async_fn, node_name))

    # Wire edges
    prev = None
    for i, mw in enumerate(middleware_list):
        node_name = f"{mw.name}.{suffix}"
        if prev is None:
            # First node in chain — already added as a graph node above;
            # its incoming edge is wired by the caller (e.g. START → first before_agent node).
            pass
        else:
            agent.add_edge(prev, node_name)
        prev = node_name

    # Wire last node to the default destination
    if prev:
        _add_middleware_edge(
            agent,
            name=prev,
            default_destination=default_destination,
            model_destination=model_destination,
            end_destination=end_destination,
            can_jump_to=None,  # type: ignore[arg-type]
        )


JSON_CODEBLOCK_PATTERN = r"(?:^|\n)```json\s*\n(.*?)```(?:\n|$)"


def _has_json_codeblock(text: str) -> bool:
    """检查文本是否包含 JSON 代码块（只匹配 ```json）。"""
    return bool(re.search(JSON_CODEBLOCK_PATTERN, text.strip(), re.DOTALL))


def _extract_json_codeblock(text: str) -> str:
    """提取最后一个 JSON 代码块的内容。"""
    matches = re.findall(JSON_CODEBLOCK_PATTERN, text, re.DOTALL)
    if not matches:
        return ""
    return matches[-1].strip()