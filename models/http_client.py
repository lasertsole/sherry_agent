"""Minimal OpenAI-compatible JSON HTTP client shared by the cloud models.

Used by ``models/embed_model/core.py`` and ``models/reranker_model/core.py``.
Both call sites post
JSON to an OpenAI-compatible endpoint with a Bearer token; this client
centralises that protocol **without** changing their per-request semantics —
no retry is added (neither call site retried) and the caller-supplied timeout
is passed through unchanged (``None`` keeps requests' default, i.e. no
timeout).

TLS verification is ON by default and only skips when the operator sets
``SHERRY_HTTP_VERIFY_TLS=0`` (see ``config.features.HTTP_CLIENT``): the
previous unconditional ``verify=False`` made every cloud call silently
MITM-able, including the ones carrying API keys.
"""

from __future__ import annotations

from typing import Any

import requests

from config.features import HTTP_CLIENT


def verify_tls() -> bool:
    """Whether outbound model requests verify TLS certificates.

    Read at call time so a ``.env`` change applies without a rebuild.
    @returns True unless the operator disabled verification.
    """
    return bool(HTTP_CLIENT["verify_tls"])


class OpenAICompatibleClient:
    """JSON POST client for an OpenAI-compatible ``api_base`` + Bearer key."""

    def __init__(self, api_base: str, api_key: str) -> None:
        """Store the base URL (trailing slash trimmed) and the auth header."""
        self._api_base = api_base.rstrip("/")
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    def post_json(
        self, path: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """POST *payload* to ``path`` and return the decoded JSON body.

        Raises ``requests.HTTPError`` on a non-2xx status (``raise_for_status``),
        matching the previous inline behavior.
        """
        resp = requests.post(
            f"{self._api_base}{path}",
            headers=self._headers,
            json=payload,
            verify=verify_tls(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
