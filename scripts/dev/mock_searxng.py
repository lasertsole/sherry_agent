#!/usr/bin/env python3
"""A minimal SearXNG JSON API stand-in for local verification.

Implements exactly what ``agent/tools/web/web_search_searxng.py`` calls:
``GET /search?q=…&format=json&pageno=1`` (plus the optional engines /
categories passthrough, echoed back in the payload so a test can assert they
arrived). Results are synthetic and deterministic:

    [1] {query} result one   (score 0.9)
    [2] {query} result two   (score 0.4)

Run:  python scripts/dev/mock_searxng.py [port]   (default 8888)

NOT part of the product: a throwaway for the 模型联网 verification, hence the
``scripts/dev/`` home. Keep it tiny.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    """Serve the one route the provider calls; everything else is a 404."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        """Answer ``/search`` with the synthetic JSON result set."""
        parsed = urlparse(self.path)
        if parsed.path != "/search":
            self._json(404, {"error": "not found"})
            return
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if params.get("format") != "json":
            # Real SearXNG answers HTML unless the JSON format is enabled; a
            # wrong request must be visible as a failure, not a silent pass.
            self._send(400, b"<html>format=json required</html>", "text/html")
            return
        query = params.get("q", "")
        payload = {
            "query": query,
            "number_of_results": 2,
            "results": [
                {
                    "title": f"{query} result one",
                    "url": f"https://example.invalid/{query}/1",
                    "content": f"First synthetic hit for {query}",
                    "score": 0.9,
                },
                {
                    "title": f"{query} result two",
                    "url": f"https://example.invalid/{query}/2",
                    "content": f"Second synthetic hit for {query}",
                    "score": 0.4,
                },
            ],
            # Echoed back so a caller can prove the passthrough reached the instance.
            "engines_requested": params.get("engines", ""),
            "categories_requested": params.get("categories", ""),
        }
        self._json(200, payload)

    def _json(self, status: int, payload: dict) -> None:
        """Send one JSON response body."""
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        """Send a response with an explicit content type and length."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Write request lines to stderr so a live check can be watched."""
        sys.stderr.write("[mock-searxng] " + (format % args) + "\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8888
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock SearXNG listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
