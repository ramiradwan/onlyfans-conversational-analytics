"""Test-only hostile port owner. Prints no received payloads."""
import asyncio
from websockets.asyncio.server import serve


async def main():
    async def hostile(ws):
        frame = await ws.recv()
        leaked = b"synthetic-message" in frame
        print("application-secret-observed=" + str(leaked).lower(), flush=True)
        await ws.send(b"not-an-authenticated-handshake")
    async with serve(hostile, "127.0.0.1", 17871, max_size=4096, compression=None):
        print("ready", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
