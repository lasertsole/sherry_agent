def escape_prompt_braces(prompt: str) -> str:
    prompt = prompt.replace('{', '{{').replace('}', '}}')

    return prompt