# Diagnostic probe: does the RUNNING backend answer ping frames?
# Read-only-ish: opens one WS, reads welcome frame, sends one inert ping, waits 3s.
import asyncio
import json

import websockets


async def probe() -> None:
    uri = "ws://127.0.0.1:8080/sessions/ws?session_id=default"
    async with websockets.connect(uri) as ws:
        try:
            welcome = await asyncio.wait_for(ws.recv(), timeout=3)
            print("WELCOME:", welcome)
        except asyncio.TimeoutError:
            print("WELCOME: <none>")

        await ws.send(json.dumps({"session_id": "default", "event": "ping", "content": ""}))
        try:
            reply = await asyncio.wait_for(ws.recv(), timeout=3)
            print("PING REPLY:", reply)
        except asyncio.TimeoutError:
            print("PING REPLY: <silence for 3s>")
        except websockets.ConnectionClosed as e:
            print("SERVER CLOSED CONNECTION ON PING:", repr(e))


asyncio.run(probe())
