#!/usr/bin/env python3
"""OpenAI-compatible mock model for the packaged-app WebDriver suite.

The real packaged backend talks to this server through ``MAIN_LLM_*`` in its
temporary ``.models`` file. It streams plain text, emits tool calls, fails a
request and paces a slow stream; the test prompt's ``[e2e:*]`` marker selects the
scenario. No API key leaves the machine and no real model is called.

Run standalone:
    python3 e2e/packaged/mock_llm.py --port 18080
    python3 e2e/packaged/mock_llm.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "mock-model"
PLAIN_CHUNKS = ("Hello ", "from the ", "packaged mock backend.")
STREAM_CHUNKS = ("Streaming ", "one ", "chunk ", "at ", "a ", "time.")
SLOW_CHUNKS = ("Still ", "working ", "on ", "the ", "answer...")
TOOLS_FINAL = "Tool result received from the real backend."
HITL_FINAL = "Approved tool finished."
MARKERS = (
    "[e2e:error]",
    "[e2e:hitl]",
    "[e2e:tools]",
    "[e2e:slow]",
    "[e2e:stream]",
    "[e2e:reasoning]",
)


def _content_text(content: Any) -> str:
    """Flatten an OpenAI message content value to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text") or content.get("content") or ""
        return text if isinstance(text, str) else ""
    return ""


def _scenario(messages: list[dict[str, Any]]) -> str:
    """The scenario named by the first/last user message marker."""
    for message in messages:
        if message.get("role") != "user":
            continue
        text = _content_text(message.get("content"))
        for marker in MARKERS:
            if marker in text:
                return marker[len("[e2e:") : -1]
    return "plain"


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    return any(message.get("role") == "tool" for message in messages)


def _tool_call(scenario: str) -> tuple[str, dict[str, Any]]:
    if scenario == "hitl":
        return "terminal", {"command": "echo e2e-approved"}
    return "read_file", {"path": "e2e-fixture.txt"}


def _final_text(scenario: str) -> str:
    return HITL_FINAL if scenario == "hitl" else TOOLS_FINAL


class Handler(BaseHTTPRequestHandler):
    """One HTTP request; streaming responses use OpenAI SSE chunks."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write("mock-llm: " + (format % args) + "\n")
        sys.stderr.flush()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, payload: dict[str, Any]) -> None:
        line = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-e2e",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish,
                }
            ],
        }

    def _stream_text(self, chunks: tuple[str, ...], delay: float) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            first = True
            for chunk in chunks:
                delta: dict[str, Any] = {"content": chunk}
                if first:
                    delta["role"] = "assistant"
                    first = False
                self._sse(self._chunk(delta))
                time.sleep(delay)
            self._sse(self._chunk({}, "stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_reasoning(self) -> None:
        """Reasoning chunks (delta.reasoning_content) followed by visible text."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            first = True
            for thought in ("Think about the question. ", "Answer is simple. "):
                delta: dict[str, Any] = {"reasoning_content": thought}
                if first:
                    delta["role"] = "assistant"
                    first = False
                self._sse(self._chunk(delta))
                time.sleep(0.2)
            self._sse(self._chunk({"content": "Reasoned reply from the mock backend."}))
            self._sse(self._chunk({}, "stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_tool(self, scenario: str) -> None:
        name, arguments = _tool_call(scenario)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self._sse(
                self._chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_e2e",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )
            )
            self._sse(self._chunk({}, "tool_calls"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _completion(
        self, body: dict[str, Any], scenario: str
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        """The non-streaming response payload for a scenario."""
        if scenario in {"tools", "hitl"} and not _has_tool_result(list(body.get("messages") or [])):
            name, arguments = _tool_call(scenario)
            return (
                "",
                "tool_calls",
                {
                    "id": "call_e2e",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                },
            )
        if scenario in {"tools", "hitl"}:
            return _final_text(scenario), "stop", None
        if scenario == "stream":
            return "".join(STREAM_CHUNKS), "stop", None
        if scenario == "slow":
            return "".join(SLOW_CHUNKS), "stop", None
        return "".join(PLAIN_CHUNKS), "stop", None

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._json(200, {"ok": True, "model": MODEL})
            return
        if self.path.rstrip("/") == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": MODEL, "object": "model", "owned_by": "e2e"}],
                },
            )
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in {"/v1/chat/completions", "/chat/completions"}:
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": {"message": "invalid JSON"}})
            return
        messages = list(body.get("messages") or [])
        scenario = _scenario(messages)
        if scenario == "error":
            # 400 is not retried by the backend's transient-error classifier, so
            # the UI gets the provider failure without waiting out 5xx backoff.
            self._json(
                400,
                {
                    "error": {
                        "message": "mock model failure for E2E",
                        "type": "invalid_request_error",
                    }
                },
            )
            return
        stream = bool(body.get("stream"))
        tool_pending = scenario in {"tools", "hitl"} and not _has_tool_result(messages)
        if stream:
            if tool_pending:
                self._stream_tool(scenario)
            elif scenario in {"tools", "hitl"}:
                # The follow-up after a tool result is still a streaming call;
                # returning a JSON body here makes LangChain raise
                # "No generations found in stream".
                self._stream_text((_final_text(scenario),), 0.1)
            elif scenario == "stream":
                self._stream_text(STREAM_CHUNKS, 0.7)
            elif scenario == "reasoning":
                self._stream_reasoning()
            elif scenario == "slow":
                self._stream_text(SLOW_CHUNKS, 1.2)
            else:
                self._stream_text(PLAIN_CHUNKS, 0.15)
            return
        content, finish, tool_call = self._completion(body, scenario)
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_call is not None:
            message["tool_calls"] = [tool_call]
        self._json(
            200,
            {
                "id": "chatcmpl-e2e",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish,
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        )


def _post(url: str, payload: dict[str, Any]) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def selftest() -> int:
    """Verify the streaming, tool-call and error contracts without the app."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}/v1/chat/completions"
    checks: dict[str, bool] = {}
    try:
        plain = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "[e2e:plain] hello"}],
            "stream": False,
        }
        status, raw = _post(base, plain)
        checks["plain"] = status == 200 and "packaged mock backend" in raw.decode("utf-8")

        tools = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "[e2e:tools] use a tool"}],
            "stream": False,
        }
        status, raw = _post(base, tools)
        payload = json.loads(raw.decode("utf-8"))
        checks["tools"] = (
            status == 200
            and payload["choices"][0]["finish_reason"] == "tool_calls"
            and payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read_file"
        )

        status, raw = _post(
            base,
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "[e2e:error] fail"}],
                "stream": False,
            },
        )
        checks["error"] = status == 400
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps({"ok": all(checks.values()), "checks": checks}))
    return 0 if all(checks.values()) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI-compatible E2E mock model")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"MOCK_LLM_READY port={server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
