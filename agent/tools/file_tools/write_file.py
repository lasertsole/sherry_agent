"""write file tool with project root restriction and autopep8 formatting for .py files."""
from typing import Any
from pathlib import Path
from config import ROOT_DIR
from pydantic import validate_call
from langchain_community.tools.file_management import WriteFileTool


def _format_py_code(text: str) -> str:
    """Format Python code using autopep8 if the content looks like valid Python."""
    try:
        import autopep8
        formatted = autopep8.fix_code(text)
        return formatted
    except Exception:
        return text


class FormattedWriteFileTool(WriteFileTool):
    """WriteFileTool that auto-formats .py files with autopep8."""

    @validate_call
    def _run(
        self,
        file_path: str,
        text: str,
        append: bool = False,
        **kwargs: Any,
    ) -> str:
        is_py = Path(file_path).suffix == ".py"
        if is_py:
            if append:
                # Append first, then format the entire file
                result = super()._run(file_path=file_path, text=text, append=True, **kwargs)
                full_text = Path(file_path).read_text(encoding="utf-8")
                formatted = _format_py_code(full_text)
                super()._run(file_path=file_path, text=formatted, append=False, **kwargs)
                return result
            else:
                # New write: format the content upfront
                text = _format_py_code(text)
                return super()._run(file_path=file_path, text=text, append=False, **kwargs)

        return super()._run(file_path=file_path, text=text, append=append, **kwargs)


def build_write_file_tool() -> WriteFileTool:
    tool = FormattedWriteFileTool(root_dir=str(ROOT_DIR))
    tool.handle_tool_error = True
    tool.name = "write_file"
    tool.metadata = {"idempotent": False}
    return tool
