"""CodeAct 工具函数：提取和合并代码块。"""

import re
import io
import ast
import builtins
import types
import contextlib
from typing import Any

# ---------------------------------------------------------------------------
# Restricted execution environment for eval_sandbox
#
# NOTE: This is an in-process *best-effort* hardening, NOT a fully robust
# sandbox. Production should run model code in a real subprocess/container
# sandbox (e.g. langchain-sandbox). The restrictions below close the obvious
# escape vectors: full ``__builtins__`` exposure, ``import``, file I/O,
# meta-programming, and the classic pure-Python ``__subclasses__()`` -> os
# introspection escape.
# ---------------------------------------------------------------------------

# Builtin names that pose an escape / side-effect risk. Anything else is kept
# so generated code behaves as before in the common case (arithmetic,
# collections, iteration, string formatting, output via print, ...).
_SANDBOX_FORBIDDEN_BUILTIN_NAMES = frozenset({
    "__import__",              # import -> os, subprocess, etc.
    "open",                    # arbitrary file read/write
    "eval", "exec", "compile", # meta code execution
    "breakpoint",              # debugger -> interactive shell
    "input",                   # blocking / interactive
    "exit", "quit",            # interpreter shutdown
    "getattr", "setattr",      # generic attribute smuggling (dunder via string)
    "globals", "locals", "vars",  # host state leak
    "help",                    # pager / interactive
    "memoryview",              # unnecessary surface
})

# Dunder attribute names that enable the classic pure-Python sandbox escape
# (``object.__subclasses__()`` -> os). Any attribute access to these is
# rejected up front by AST inspection. ``__getattribute__`` is included so
# ``obj.__getattribute__('__subclasses__')`` cannot smuggle past the gate.
_SANDBOX_FORBIDDEN_DUNDER_ATTRS = frozenset({
    "__class__", "__subclasses__", "__globals__", "__code__",
    "__bases__", "__mro__", "__import__", "__reduce__", "__func__",
    "__closure__", "__getattribute__",
})

# Sanitized globals dict: derived from real builtins minus the forbidden set.
# ``__builtins__`` is explicitly set to this SAME filtered dict so CPython's
# ``exec``/``eval`` do NOT re-inject the full ``builtins`` module underneath
# our back (which would nullify the whole whitelist).
SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(builtins, name)
    for name in dir(builtins)
    if name not in _SANDBOX_FORBIDDEN_BUILTIN_NAMES
}
SAFE_BUILTINS["__builtins__"] = SAFE_BUILTINS


def _assert_safe_ast(tree: ast.AST) -> None:
    """Raise if code uses forbidden escapes (import / dangerous dunders / names).

    Rejects:
      - ``import`` / ``from ... import ...`` statements
      - attribute access to dangerous dunders (``obj.__subclasses__`` ...)
      - any *direct* reference to a forbidden builtin name (``open``, ``eval``,
        ``exec``, ``compile``, ...), so these fail with a clear message instead
        of a bare ``NameError`` and cannot recur if one is later re-added.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("import is not allowed in the restricted environment")
        if isinstance(node, ast.Attribute) and node.attr in _SANDBOX_FORBIDDEN_DUNDER_ATTRS:
            raise ValueError(f"attribute access to '{node.attr}' is not allowed")
        if isinstance(node, ast.Name) and node.id in _SANDBOX_FORBIDDEN_BUILTIN_NAMES:
            raise ValueError(f"name '{node.id}' is not allowed in the restricted environment")


BACKTICK_PATTERN = r"(?:^|\n)```(?:.*?\n)?(.*?)(?:```(?:\n|$))"


def extract_and_combine_codeblocks(text: str) -> str:
    """
    Extracts all codeblocks from a text string and combines them into a single code string.

    Args:
        text: A string containing zero or more codeblocks, where each codeblock is
            surrounded by triple backticks (```).

    Returns:
        A string containing the combined code from all codeblocks, with each codeblock
        separated by a newline.
    """
    code_blocks = re.findall(BACKTICK_PATTERN, text, re.DOTALL)

    if not code_blocks:
        return ""

    # 清理每个代码块的前后空白
    processed_blocks = [block.strip() for block in code_blocks]
    combined_code = "\n\n".join(processed_blocks)
    return combined_code

def eval_sandbox(code: str, _locals: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    在受限环境中执行 Python 代码。

    注意：生产环境应使用真正的沙箱（如 langchain-sandbox）。
    此实现仅用于测试目的。
    """
    original_keys = set(_locals.keys())

    try:
        need_eval = False
        parsed = None
        try:
            parsed = ast.parse(code.strip(), mode="exec")
            if (
                len(parsed.body) == 1
                and isinstance(parsed.body[0], ast.Expr)
                and isinstance(parsed.body[0].value, ast.Call)
            ):
                call_node = parsed.body[0].value
                func_name = None
                if isinstance(call_node.func, ast.Name):
                    func_name = call_node.func.id
                elif isinstance(call_node.func, ast.Attribute):
                    func_name = call_node.func.attr
                # 仅当调用的函数在 _locals 中（即工具函数）时走 eval
                if func_name and func_name in _locals and not func_name in dir(builtins):
                    need_eval = True
        except SyntaxError:
            parsed = None

        # Reject forbidden escapes (import / dangerous dunders) before running.
        if parsed is not None:
            _assert_safe_ast(parsed)

        if need_eval:
            result = eval(code.strip(), SAFE_BUILTINS, _locals)
            if result is None:
                result = "<tool returned None>"
            else:
                result = repr(result)
        else:
            with contextlib.redirect_stdout(io.StringIO()) as f:
                exec(code, SAFE_BUILTINS, _locals)
            result = f.getvalue()
            if not result:
                # 检查是否有新定义的变量（非函数、非模块）作为可能的工具返回值
                new_keys = set(_locals.keys()) - original_keys
                new_vars = {key: _locals[key] for key in new_keys}
                new_values = []
                for k, v in new_vars.items():
                    if not callable(v) and not isinstance(v, type(builtins)):
                        new_values.append(f"{k} = {repr(v)[:500]}")
                if new_values:
                    result = "\n".join(new_values)
                else:
                    result = "<code ran, no output printed to stdout>"
    except Exception as e:
        result = f"Error during execution: {repr(e)}"

    new_keys = set(_locals.keys()) - original_keys
    new_vars = {key: _locals[key] for key in new_keys}

    # 过滤掉不可被 msgpack 序列化的变量（如 module、open file handle 等），
    # 防止它们流入 state.context 导致 LangGraph checkpoint 的 ormsgpack.packb() 失败。
    _SERIALIZABLE_TYPES = (
        str, int, float, bool, type(None),
        bytes, bytearray,
        list, tuple, dict, set, frozenset,
    )
    # msgpack 仅支持 64-bit 有符号整数（-2^63 ~ 2^63-1），
    # Python 无限精度 int 超出此范围会导致或msgpack.packb() 抛出 TypeError。
    # 此处统一将所有超出 64-bit 的 int 转为 str 以防止 checkpoint 序列化崩溃。
    _MAX_SAFE_INT = 2**63 - 1
    _MIN_SAFE_INT = -(2**63)

    filtered = {}
    for k, v in new_vars.items():
        if isinstance(v, _SERIALIZABLE_TYPES):
            # int 类型需要额外检查是否超出 64-bit 范围
            if isinstance(v, int) and not isinstance(v, bool) and (v > _MAX_SAFE_INT or v < _MIN_SAFE_INT):
                filtered[k] = str(v)
            else:
                filtered[k] = v
        elif isinstance(v, (types.ModuleType, io.IOBase)):
            # module 和 file handle 不能序列化 → 丢弃
            continue
        elif callable(v):
            # 函数/类 → 丢弃
            continue
        elif isinstance(v, type):
            # 类型对象 → 丢弃
            continue
        else:
            # 其他类型尝试 repr 兜底
            try:
                import ormsgpack
                ormsgpack.packb(v)
                filtered[k] = v
            except Exception:
                # 不能序列化 → 保存 repr 字符串
                filtered[k] = repr(v)

    return result, filtered