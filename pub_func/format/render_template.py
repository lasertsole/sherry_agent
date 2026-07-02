"""
Template renderer — replaces ``{{ variable }}`` placeholders in templates with actual values.
"""

import re
from typing import Any


def template_render(template_content: str, variables: dict[str, Any]) -> str:
    """
    Replace ``{{ variable }}`` placeholders in ``template_content`` with the given values.

    Args:
        template_content: Template string containing ``{{ variable }}`` placeholders.
        variables: Mapping from variable name to value.

    Returns:
        The rendered string with placeholders replaced.

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
            # Convert to string preserving the original type's appearance
            return str(value) if value is not None else ""
        # If the variable is not found, leave the placeholder as-is
        return match.group(0)

    # Match {{ variable }} patterns (tolerant of surrounding whitespace)
    pattern = r'\{\{\s*(.*?)\s*\}\}'
    result = re.sub(pattern, replace_match, template_content)

    return result


def render_template_file(file_path: str, variables: dict[str, Any]) -> str:
    """
    Read a template file and render it with the given variables.

    Args:
        file_path: Path to the template file.
        variables: Mapping from variable name to value.

    Returns:
        Rendered string.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        template_content = f.read()

    return template_render(template_content, variables)
