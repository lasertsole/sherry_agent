from typing import Any, List

def process_sse_data(data: Any)-> str:
    res_lines: List[str] =  []
    if data:
        decoded_line: str = data if isinstance(data, str) else data.decode()
        decoded_lines: List[str] = decoded_line.splitlines()
        for line in decoded_lines:
            if line.startswith("data: "):
                res:str = line[6:].strip()
                res_lines.append(res)

    return "\n".join(res_lines)