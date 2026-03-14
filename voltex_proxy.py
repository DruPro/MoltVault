"""
voltex_proxy.py  —  WebSocket ↔ TCP bridge
Reads config from environment variables so it works both locally
and inside Docker without code changes.

  VOLTEX_HOST  — vault server host  (default: localhost)
  VOLTEX_PORT  — vault server port  (default: 7474)
  WS_HOST      — bind address       (default: localhost  →  0.0.0.0 in Docker)
  WS_PORT      — WebSocket port     (default: 8765)
"""
import asyncio, os
import websockets

VOLTEX_HOST = os.getenv("VOLTEX_HOST", "localhost")
VOLTEX_PORT = int(os.getenv("VOLTEX_PORT", 7474))
WS_HOST     = os.getenv("WS_HOST",     "localhost")
WS_PORT     = int(os.getenv("WS_PORT", 8765))

async def handle(ws):
    reader, writer = await asyncio.open_connection(VOLTEX_HOST, VOLTEX_PORT)
    async def tcp_to_ws():
        async for line in reader:
            await ws.send(line.decode().strip())
    fwd = asyncio.ensure_future(tcp_to_ws())
    async for msg in ws:
        writer.write((msg.strip() + "\n").encode())
        await writer.drain()
    fwd.cancel()
    writer.close()

async def main():
    print(f"Voltex WS proxy  ws://{WS_HOST}:{WS_PORT}  →  tcp://{VOLTEX_HOST}:{VOLTEX_PORT}")
    async with websockets.serve(handle, WS_HOST, WS_PORT):
        await asyncio.Future()

asyncio.run(main())
