from typing import Any

def process_sse_data(data: Any)-> str:
    res_lines: list[str] =  []
    if data:
        decoded_line: str = data if isinstance(data, str) else data.decode()
        decoded_lines: list[str] = decoded_line.splitlines()
        for line in decoded_lines:
            if line.startswith("data: "):
                res:str = line[6:].strip()
                res_lines.append(res)

    return "\n".join(res_lines)