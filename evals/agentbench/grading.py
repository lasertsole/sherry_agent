"""AgentBench DBBench's official answer comparison, ported verbatim.

Source: ``src/server/tasks/dbbench/result_processor.py`` at the pinned
AgentBench commit
(https://github.com/THUDM/AgentBench/blob/d1e4a10db08c87075c78972e48ecc182be03e2d5/src/server/tasks/dbbench/result_processor.py;
see ``instances.AGENTBENCH_PIN``). The comparison rules —
special-value normalisation, percentage / thousands-separator cleanup, float
tolerance, and set comparison for multi-value answers — are the benchmark's
own verdict, not a new grader. ``eval`` in the original is replaced by
``ast.literal_eval``; the benchmark's answer strings are Python literals, so the
result is identical and the grader is not exposed to arbitrary input.
"""

from __future__ import annotations

import ast
from typing import Any

OFFICIAL_SYSTEM_PROMPT = """I will ask you a question, then you should help me operate a MySQL database with SQL to answer the question.
You have to explain the problem and your solution to me and write down your thoughts.
After thinking and explaining thoroughly, every round you can choose to operate or to answer with the two specific tools provided.
If you should execute a SQL query, use the `execute_sql` function, Your SQL should be in one line.
Every time you can only execute one SQL statement. I will only execute the statement in the first SQL code block. Every time you write a SQL, I will execute it for you and give you the output.
If you are done operating, and you want to commit your final answer, then use the `commit_final_answer` function.
DO NOT use this tool unless you are sure about your answer. I expect an accurate and correct answer.
Your answer should be accurate. Your answer must be exactly the same as the correct answer.
If the question is about modifying the database, then after done operation, your answer field can be anything.
If your response cannot match any pattern I mentioned earlier, you will be judged as FAIL immediately.
You should always use the tools provided to submit your answer. Be careful not to write it in the content field.
Your input will be raw MySQL response, you have to deal with it by yourself."""


def _normalize_special_values(value: Any) -> str:
    """Handle special values, percentages and formatted numbers."""
    if value is None:
        return "0"
    text = str(value).strip()
    if text.endswith("%"):
        return text[:-1].strip()
    if "," in text and not text.startswith("[") and not text.endswith("]"):
        text = text.replace(",", "")
    return {
        "none": "0",
        "null": "0",
        "undefined": "0",
        "nan": "0",
        "inf": "0",
        "infinity": "0",
        "-inf": "0",
        "-infinity": "0",
        "": "0",
    }.get(text.lower(), text)


def _literal(text: str) -> Any:
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _clean_mysql_result(result: Any) -> list[str] | None:
    """Normalise ``[(value,)]``-shaped MySQL result strings."""
    if not (isinstance(result, str) and result.startswith("[") and result.endswith("]")):
        return None
    parsed = _literal(result)
    if isinstance(parsed, list) and all(isinstance(item, tuple) for item in parsed):
        cleaned: list[str] = []
        for item in parsed:
            if len(item) == 1:
                cleaned.append(str(item[0]).strip().strip("'\""))
        if cleaned:
            return cleaned
    stripped = result.strip("[]")
    if stripped.count("(") == 1 and stripped.startswith("(") and stripped.endswith(",)"):
        return [stripped[1:-2].strip().strip("'\"")]
    return None


def _clean_answer(answer: Any) -> list[str]:
    """Normalise one answer (official ``_clean_answer``)."""
    if answer is None:
        return ["0"]
    mysql_result = _clean_mysql_result(answer)
    if mysql_result is not None:
        return [_normalize_special_values(value) for value in mysql_result]

    if isinstance(answer, str):
        answer = answer.strip()
        if answer.startswith("[") and answer.endswith("]"):
            parsed = _literal(answer)
            if isinstance(parsed, list):
                cleaned: list[str] = []
                for item in parsed:
                    value = item[0] if isinstance(item, tuple) and len(item) == 1 else item
                    cleaned.append(_normalize_special_values(str(value).strip().strip("'\"")))
                return cleaned
            # Manual split, mirroring the official fallback.
            answer = answer[1:-1]
            items: list[str] = []
            current = ""
            in_quotes = False
            for char in answer:
                if char in "\"'":
                    in_quotes = not in_quotes
                elif char == "," and not in_quotes:
                    if current:
                        items.append(_normalize_special_values(current.strip().strip("'\"")))
                        current = ""
                else:
                    current += char
            if current:
                items.append(_normalize_special_values(current.strip().strip("'\"")))
            return items
        return [_normalize_special_values(answer.strip().strip("'\""))]

    if isinstance(answer, (list, tuple)):
        cleaned = []
        for item in answer:
            value = item[0] if isinstance(item, tuple) and len(item) == 1 else item
            cleaned.append(_normalize_special_values(str(value).strip().strip("'\"")))
        return cleaned

    return [_normalize_special_values(str(answer).strip().strip("'\""))]


def _is_float(value: Any) -> bool:
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def _float_equal(left: Any, right: Any, tol: float = 1e-2) -> bool:
    try:
        return abs(float(left) - float(right)) <= tol
    except (ValueError, TypeError):
        return False


def compare_results(answer: Any, ground_truth: Any, query_type: str) -> bool:
    """The official DBBench verdict for one answer."""
    try:
        processed_answer = _clean_answer(answer)
        processed_ground_truth = _clean_answer(ground_truth)
        if str(query_type).upper() in ("INSERT", "DELETE", "UPDATE"):
            return processed_answer == processed_ground_truth

        if len(processed_answer) == 1 and len(processed_ground_truth) == 1:
            ans_val = processed_answer[0]
            gt_val = processed_ground_truth[0]
            if ans_val == "0" and gt_val == "0":
                return True
            if _is_float(ans_val) and _is_float(gt_val):
                return _float_equal(ans_val, gt_val)
            return ans_val == gt_val

        if all(_is_float(value) for value in processed_answer) and all(
            _is_float(value) for value in processed_ground_truth
        ):
            if len(processed_answer) != len(processed_ground_truth):
                return False
            matched = [False] * len(processed_ground_truth)
            for value in processed_answer:
                for index, target in enumerate(processed_ground_truth):
                    if not matched[index] and _float_equal(value, target):
                        matched[index] = True
                        break
                else:
                    return False
            return all(matched)

        return set(processed_answer) == set(processed_ground_truth)
    except Exception:  # noqa: BLE001 - the official grader is fail-closed
        return False
