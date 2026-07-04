"""Fuzzy text matching and replacement engine.

Provides multi-strategy fuzzy find-and-replace, fallback "did you mean?"
closest-line hints, and all internal helper functions.
"""

import re
from collections.abc import Callable
from difflib import SequenceMatcher


_UNICODE_MAP = {
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
    "\u2014": "--", "\u2013": "-",
    "\u2026": "...", "\u00a0": " ",
}


def _line_positions(lines: list[str], start: int, end: int, total_len: int) -> tuple[int, int]:
    sp = sum(len(l) + 1 for l in lines[:start])
    ep = sum(len(l) + 1 for l in lines[:end]) - 1
    return sp, min(ep, total_len)


def _find_normalized_matches(
    content: str, content_lines: list[str],
    content_norm_lines: list[str], pattern_norm: str,
) -> list[tuple[int, int]]:
    plines = pattern_norm.split('\n')
    plen = len(plines)
    matches = []
    for i in range(len(content_norm_lines) - plen + 1):
        if '\n'.join(content_norm_lines[i:i + plen]) == pattern_norm:
            sp, ep = _line_positions(content_lines, i, i + plen, len(content))
            matches.append((sp, ep))
    return matches


def _strategy_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + 1
    return matches


def _strategy_line_trimmed(content: str, pattern: str) -> list[tuple[int, int]]:
    pattern_lines = [line.strip() for line in pattern.split('\n')]
    pattern_normalized = '\n'.join(pattern_lines)
    content_lines = content.split('\n')
    content_normalized_lines = [line.strip() for line in content_lines]
    return _find_normalized_matches(content, content_lines, content_normalized_lines, pattern_normalized)


def _map_whitespace_positions(
    original: str, normalized: str,
    norm_matches: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if not norm_matches:
        return []
    orig_to_norm: list[int] = []
    oi, ni = 0, 0
    while oi < len(original) and ni < len(normalized):
        if original[oi] == normalized[ni]:
            orig_to_norm.append(ni)
            oi += 1
            ni += 1
        elif original[oi] in ' \t' and normalized[ni] == ' ':
            orig_to_norm.append(ni)
            oi += 1
            if oi < len(original) and original[oi] not in ' \t':
                ni += 1
        elif original[oi] in ' \t':
            orig_to_norm.append(ni)
            oi += 1
        else:
            orig_to_norm.append(ni)
            oi += 1
    while oi < len(original):
        orig_to_norm.append(len(normalized))
        oi += 1

    n2o_start: dict[int, int] = {}
    n2o_end: dict[int, int] = {}
    for opos, npos in enumerate(orig_to_norm):
        if npos not in n2o_start:
            n2o_start[npos] = opos
        n2o_end[npos] = opos

    result = []
    for ns, ne in norm_matches:
        os_ = n2o_start.get(ns, min(i for i, n in enumerate(orig_to_norm) if n >= ns))
        oe = n2o_end.get(ne - 1, os_ + (ne - ns)) + 1 if ne - 1 in n2o_end else os_ + (ne - ns)
        if ne < len(normalized) and normalized[ne - 1] == ' ':
            while oe < len(original) and original[oe] in ' \t':
                oe += 1
        result.append((os_, min(oe, len(original))))
    return result


def _strategy_whitespace_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    normalize = lambda s: re.sub(r'[ \t]+', ' ', s)
    pattern_normalized = normalize(pattern)
    content_normalized = normalize(content)
    norm_matches = _strategy_exact(content_normalized, pattern_normalized)
    if not norm_matches:
        return []
    return _map_whitespace_positions(content, content_normalized, norm_matches)


def _strategy_indentation_flexible(content: str, pattern: str) -> list[tuple[int, int]]:
    content_lines = content.split('\n')
    content_stripped = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split('\n')]
    return _find_normalized_matches(content, content_lines, content_stripped, '\n'.join(pattern_lines))


def _strategy_escape_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    unescaped = pattern.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    if unescaped == pattern:
        return []
    return _strategy_exact(content, unescaped)


def _strategy_trimmed_boundary(content: str, pattern: str) -> list[tuple[int, int]]:
    pattern_lines = pattern.split('\n')
    if not pattern_lines:
        return []
    pattern_lines[0] = pattern_lines[0].strip()
    if len(pattern_lines) > 1:
        pattern_lines[-1] = pattern_lines[-1].strip()
    modified_pattern = '\n'.join(pattern_lines)
    content_lines = content.split('\n')
    plen = len(pattern_lines)
    matches = []
    for i in range(len(content_lines) - plen + 1):
        block = content_lines[i:i + plen]
        check = block.copy()
        check[0] = check[0].strip()
        if len(check) > 1:
            check[-1] = check[-1].strip()
        if '\n'.join(check) == modified_pattern:
            start_pos, end_pos = _line_positions(content_lines, i, i + plen, len(content))
            matches.append((start_pos, end_pos))
    return matches


def _unicode_normalize(text: str) -> str:
    for char, repl in _UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text


def _build_orig_to_norm_map(original: str) -> list[int]:
    result: list[int] = []
    norm_pos = 0
    for char in original:
        result.append(norm_pos)
        repl = _UNICODE_MAP.get(char)
        norm_pos += len(repl) if repl is not None else 1
    result.append(norm_pos)
    return result


def _strategy_unicode_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    if norm_content == content and norm_pattern == pattern:
        return []
    norm_matches = _strategy_exact(norm_content, norm_pattern)
    if not norm_matches:
        norm_matches = _strategy_line_trimmed(norm_content, norm_pattern)
    if not norm_matches:
        return []
    orig_to_norm = _build_orig_to_norm_map(content)
    return _map_norm_to_orig(orig_to_norm, norm_matches)


def _map_norm_to_orig(
    orig_to_norm: list[int], norm_matches: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    n2o: dict[int, int] = {}
    for opos, npos in enumerate(orig_to_norm[:-1]):
        if npos not in n2o:
            n2o[npos] = opos
    results: list[tuple[int, int]] = []
    olen = len(orig_to_norm) - 1
    for ns, ne in norm_matches:
        if ns not in n2o:
            continue
        os_ = n2o[ns]
        oe = os_
        while oe < olen and orig_to_norm[oe] < ne:
            oe += 1
        results.append((os_, oe))
    return results


def _strategy_block_anchor(content: str, pattern: str) -> list[tuple[int, int]]:
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    pattern_lines = norm_pattern.split('\n')
    if len(pattern_lines) < 2:
        return []
    first_line = pattern_lines[0].strip()
    last_line = pattern_lines[-1].strip()
    norm_content_lines = norm_content.split('\n')
    orig_content_lines = content.split('\n')
    plen = len(pattern_lines)
    potential = [
        i for i in range(len(norm_content_lines) - plen + 1)
        if norm_content_lines[i].strip() == first_line
        and norm_content_lines[i + plen - 1].strip() == last_line
    ]
    threshold = 0.50 if len(potential) <= 1 else 0.70
    matches = []
    for i in potential:
        if plen <= 2:
            sim = 1.0
        else:
            cm = '\n'.join(norm_content_lines[i + 1:i + plen - 1])
            pm = '\n'.join(pattern_lines[1:-1])
            sim = SequenceMatcher(None, cm, pm).ratio()
        if sim >= threshold:
            sp, ep = _line_positions(orig_content_lines, i, i + plen, len(content))
            matches.append((sp, ep))
    return matches


def _strategy_context_aware(content: str, pattern: str) -> list[tuple[int, int]]:
    pattern_lines = pattern.split('\n')
    content_lines = content.split('\n')
    if not pattern_lines:
        return []
    plen = len(pattern_lines)
    matches = []
    for i in range(len(content_lines) - plen + 1):
        block = content_lines[i:i + plen]
        hi = sum(
            1 for pl, cl in zip(pattern_lines, block)
            if SequenceMatcher(None, pl.strip(), cl.strip()).ratio() >= 0.80
        )
        if hi >= len(pattern_lines) * 0.5:
            sp, ep = _line_positions(content_lines, i, i + plen, len(content))
            matches.append((sp, ep))
    return matches


_STRATEGIES: list[tuple[str, Callable[[str, str], list[tuple[int, int]]]]] = [
    ("exact", _strategy_exact),
    ("line_trimmed", _strategy_line_trimmed),
    ("whitespace_normalized", _strategy_whitespace_normalized),
    ("indentation_flexible", _strategy_indentation_flexible),
    ("escape_normalized", _strategy_escape_normalized),
    ("trimmed_boundary", _strategy_trimmed_boundary),
    ("unicode_normalized", _strategy_unicode_normalized),
    ("block_anchor", _strategy_block_anchor),
    ("context_aware", _strategy_context_aware),
]


def _detect_escape_drift(content: str, matches: list[tuple[int, int]],
                         old_string: str, new_string: str) -> str | None:
    if "\\'" not in new_string and '\\"' not in new_string:
        return None
    matched = "".join(content[s:e] for s, e in matches)
    for suspect in ("\\'", '\\"'):
        if suspect in new_string and suspect in old_string and suspect not in matched:
            return (
                f"Escape-drift detected: old_string and new_string contain "
                f"{suspect!r} but the matched file region does not. "
                "Re-read the file and pass old_string/new_string without "
                f"backslash-escaping {suspect[1]!r}."
            )
    return None


def _first_meaningful_line(text: str) -> str | None:
    for line in text.split('\n'):
        if line.strip():
            return line
    return None


def _leading_ws(line: str) -> str:
    i = 0
    while i < len(line) and line[i] in (' ', '\t'):
        i += 1
    return line[:i]


def _reindent(file_region: str, old_string: str, new_string: str) -> str:
    if not new_string:
        return new_string
    old_first = _first_meaningful_line(old_string)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string
    old_indent = _leading_ws(old_first)
    file_indent = _leading_ws(file_first)
    if old_indent == file_indent:
        return new_string
    out: list[str] = []
    for line in new_string.split('\n'):
        if not line.strip():
            out.append(line)
            continue
        li = _leading_ws(line)
        if li.startswith(old_indent):
            out.append(file_indent + line[len(old_indent):])
        else:
            out.append(file_indent + line.lstrip(' \t'))
    return '\n'.join(out)


def fuzzy_find_and_replace(
    content: str, old_string: str, new_string: str, replace_all: bool = False,
) -> tuple[str, int, str | None, str | None]:
    """Return (new_content, match_count, strategy_name, error)."""
    if not old_string:
        return content, 0, None, "old_string cannot be empty"
    if old_string == new_string:
        return content, 0, None, "old_string and new_string are identical"

    for name, fn in _STRATEGIES:
        matches = fn(content, old_string)
        if not matches:
            continue
        if len(matches) > 1 and not replace_all:
            return content, 0, None, (
                f"Found {len(matches)} matches for old_string. "
                "Provide more context to make it unique, or use replace_all=True."
            )
        if name != "exact":
            drift_err = _detect_escape_drift(content, matches, old_string, new_string)
            if drift_err:
                return content, 0, None, drift_err

        effective = new_string
        result = content
        sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)
        for start, end in sorted_matches:
            if name != "exact":
                adjusted = _reindent(content[start:end], old_string, effective)
            else:
                adjusted = effective
            result = result[:start] + adjusted + result[end:]
        return result, len(matches), name, None

    return content, 0, None, "Could not find a match for old_string in the file"


def find_closest_lines(old_string: str, content: str, context_lines: int = 2, max_results: int = 3) -> str:
    """Find lines in content most similar to old_string for "did you mean?" feedback.

    Returns a formatted string showing the closest matching lines with context,
    or empty string if no useful match is found.
    """
    if not old_string or not content:
        return ""

    old_lines = old_string.splitlines()
    content_lines = content.splitlines()

    if not old_lines or not content_lines:
        return ""

    # Use first line of old_string as anchor for search
    anchor = old_lines[0].strip()
    if not anchor:
        # Try second line if first is blank
        candidates = [l.strip() for l in old_lines if l.strip()]
        if not candidates:
            return ""
        anchor = candidates[0]

    # Score each line in content by similarity to anchor
    scored = []
    for i, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = SequenceMatcher(None, anchor, stripped).ratio()
        if ratio > 0.3:
            scored.append((ratio, i))

    if not scored:
        return ""

    # Take top matches
    scored.sort(key=lambda x: -x[0])
    top = scored[:max_results]

    parts = []
    seen_ranges = set()
    for _, line_idx in top:
        start = max(0, line_idx - context_lines)
        end = min(len(content_lines), line_idx + len(old_lines) + context_lines)
        key = (start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        snippet = "\n".join(
            f"{start + j + 1:4d}| {content_lines[start + j]}"
            for j in range(end - start)
        )
        parts.append(snippet)

    if not parts:
        return ""

    return "\n---\n".join(parts)


def format_no_match_hint(error: str | None, match_count: int,
                         old_string: str, content: str) -> str:
    """Return a '\\n\\nDid you mean...' snippet for plain no-match errors.

    Gated so the hint only fires for actual "old_string not found" failures.
    Ambiguous-match ("Found N matches"), escape-drift, and identical-strings
    errors all have ``match_count == 0`` but a "did you mean?" snippet would
    be misleading — those failed for unrelated reasons.

    Returns an empty string when there's nothing useful to append.
    """
    if match_count != 0:
        return ""
    if not error or not error.startswith("Could not find"):
        return ""
    hint = find_closest_lines(old_string, content)
    if not hint:
        return ""
    return "\n\nDid you mean one of these sections?\n" + hint
