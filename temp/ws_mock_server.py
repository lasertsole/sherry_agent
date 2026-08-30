"""E2E 专用 mock 后端 WS 服务（模拟 Robyn /sessions/ws 协议）。

行为：
- 连接建立后发送欢迎帧 {"content": "websocket connected successfully"}
- 收到任何消息后回一帧 {"event": "pong"}

挂死模拟：存在 temp/ws_mock_mute.flag 时，已建立的连接保持打开但不回任何帧
（模拟进程事件循环卡死：TCP 未断、无响应），用于验证前端心跳 pong 超时判死逻辑。
"""
import asyncio
import json
import os

import websockets

PORT = 8090
_HERE = os.path.dirname(os.path.abspath(__file__))
MUTE_FLAG = os.path.join(_HERE, "ws_mock_mute.flag")


def muted() -> bool:
    return os.path.exists(MUTE_FLAG)


async def handler(ws) -> None:
    print(f"[conn] client connected (muted={muted()})", flush=True)
    if not muted():
        await ws.send(json.dumps({"content": "websocket connected successfully"}))
    try:
        async for message in ws:
            if muted():
                # 挂死模拟：连接保持，但不回任何帧
                print("[mute] frame received, staying silent", flush=True)
                continue
            try:
                obj = json.loads(message)
            except Exception:
                obj = {"raw": str(message)[:100]}
            print(f"[recv] {obj}", flush=True)
            await ws.send(json.dumps({"event": "pong"}))
    except websockets.ConnectionClosed as e:
        print(f"[closed] code={e.code} rcvd={e.rcvd} sent={e.sent}", flush=True)


async def main() -> None:
    async with websockets.serve(handler, "127.0.0.1", PORT):
        print(f"[ready] mock WS backend on ws://127.0.0.1:{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
