"""Real-image upload test over WS to verify media persistence.

Sends a real base64 image to /sessions/agent/ws, reads a bounded number of
chunks (triggering at least one before_agent/after_agent cycle), then issues
type:"stop" to terminate the generation cleanly.
"""
import asyncio
import base64
import json
import sys

import websockets

WS_URL = "ws://127.0.0.1:8080/sessions/agent/ws"
IMAGE_PATH = r"C:\app\code\project\EMA_AI_agent\src\main\mutil_temp\1786454044664.jpg"
SESSION_ID = "main"
MAX_CHUNKS = 200
STOP_AFTER_TOOL = True  # stop once the first model iteration ends (tool call marker)


def load_image_base64() -> str:
    with open(IMAGE_PATH, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


async def main() -> None:
    image_b64 = load_image_base64()
    print(f"[client] image bytes={len(base64.b64decode(image_b64))}")

    async with websockets.connect(WS_URL, max_size=50_000_000) as ws:
        payload = {
            "session_id": SESSION_ID,
            "multi_modal_message": {
                "text": "描述一下这张图片",
                "image_base64_list": [image_b64],
            },
        }
        print("[client] sending request")
        await ws.send(json.dumps(payload))

        recv_count = 0
        saw_done = False
        acc = []
        try:
            while recv_count < MAX_CHUNKS:
                raw = await asyncio.wait_for(ws.recv(), timeout=120.0)
                recv_count += 1
                evt = json.loads(raw)
                etype = evt.get("event")
                content = evt.get("content", "")
                preview = content[:60] if isinstance(content, str) else content
                print(f"[client] recv[{recv_count}] event={etype} content={preview!r}")
                if etype in ("done", "error"):
                    saw_done = True
                    print("[client] got done/error -> full turn ended")
                    break
                if etype == "chunk":
                    if content.startswith("Sherry:"):
                        continue
                    if content.startswith("\n\n**Calling tool") or content.startswith("**[Calling"):
                        print("[client] tool call marker, continuing through iterations")
                        continue
                    acc.append(content)
                    # A real final-answer text chunk (not a tool marker).
                    if len(content) > 40 and not content.startswith("\n\n**"):
                        print("[client] got substantial final-answer response")
                        break
        except asyncio.TimeoutError:
            print(f"[client] timed out after {recv_count} chunks (acc_len={len(''.join(acc))})")
        finally:
            print("[client] sending stop")
            await ws.send(json.dumps({"type": "stop", "session_id": SESSION_ID}))
            # Read a short confirmation if available.
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                print(f"[client] post-stop recv: {raw[:120]!r}")
            except Exception:
                print("[client] no post-stop message")

    print("[client] done")


if __name__ == "__main__":
    asyncio.run(main())
