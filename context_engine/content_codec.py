"""Shared JSON content-cell decoder.

The four decode call sites — ``context_engine/core.py::_decode_content``,
``context_engine/store/core.py::_decode_json_columns`` and
``::_decode_title_content``, and ``context_engine/embeddings/store.py::_decode``
— differ only in the failure mode (return the raw value vs propagate), in
whether the ``\\x00json:`` marker gates decoding, and in what happens to the
decoded value afterwards, so the decode core lives here and each site keeps its
own marker check / post-processing.
"""

from __future__ import annotations

import json
from typing import Any


def decode_content(
    value: Any,
    *,
    prefix: str | None = None,
    strict: bool = False,
) -> Any:
    """Decode *value* when it is a JSON-encoded string.

    Non-string values are returned unchanged. When *prefix* is given, only
    strings carrying it are decoded (the payload after the prefix); every other
    string passes through untouched. Malformed JSON returns the original string,
    or re-raises when *strict* is set.
    """
    if not isinstance(value, str):
        return value
    if prefix is not None:
        if not value.startswith(prefix):
            return value
        payload = value[len(prefix) :]
    else:
        payload = value
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        if strict:
            raise
        return value
