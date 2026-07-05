"""CodeAct 工具函数：提取和合并代码块。"""

import re
import io
import ast
import builtins
import types
import contextlib
from typing import Any

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
            pass

        if need_eval:
            result = eval(code.strip(), builtins.__dict__, _locals)
            if result is None:
                result = "<tool returned None>"
            else:
                result = repr(result)
        else:
            with contextlib.redirect_stdout(io.StringIO()) as f:
                exec(code, builtins.__dict__, _locals)
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
    filtered = {}
    for k, v in new_vars.items():
        if isinstance(v, _SERIALIZABLE_TYPES):
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