"""
模板渲染工具 - 将模板中的 {{ variable }} 占位符替换为实际值
"""

from typing import Dict, Any
import re


def template_render(template_content: str, variables: Dict[str, Any]) -> str:
    """
    将模板字符串中的 {{ variable }} 占位符替换为指定值

    Args:
        template_content: 包含 {{ variable }} 占位符的模板字符串
        variables: 变量名到值的映射字典

    Returns:
        替换后的字符串

    Example:
        >>> template = "Hello {{ name }}, you are {{ age }} years old"
        >>> result = template_render(template, {"name": "Alice", "age": 25})
        >>> print(result)
        Hello Alice, you are 25 years old
    """

    def replace_match(match):
        var_name = match.group(1).strip()
        if var_name in variables:
            value = variables[var_name]
            # 转换为字符串，保持原始类型的外观
            return str(value) if value is not None else ""
        # 如果变量不存在，保留原样
        return match.group(0)

    # 匹配 {{ variable }} 模式（支持前后空格）
    pattern = r'\{\{\s*(.*?)\s*\}\}'
    result = re.sub(pattern, replace_match, template_content)

    return result


def render_template_file(file_path: str, variables: Dict[str, Any]) -> str:
    """
    读取模板文件并渲染变量

    Args:
        file_path: 模板文件路径
        variables: 变量名到值的映射字典

    Returns:
        渲染后的字符串
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        template_content = f.read()

    return template_render(template_content, variables)
