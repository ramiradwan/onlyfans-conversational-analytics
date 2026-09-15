#!/usr/bin/env python3
"""Independent Python peer for the isolated snow/WASM feasibility spike."""
import argparse, asyncio, json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[3]
SPIKE = ROOT / 'tools' / 'companion-session-spike'
sys.path.insert(0, str(SPIKE))
from session import Session, establish  # noqa: E402

AGENT_PRIVATE = bytes.fromhex('101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f')
AGENT_PUBLIC = bytes.fromhex('d89e3bad79437dbed9f843418304f460ff05c7fe81fe4a9577a804cb9367ff66')
BRAIN_PRIVATE = bytes.fromhex('404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f')
BRAIN_PUBLIC = bytes.fromhex('79a631eede1bf9c98f12032cdeadd0e7a079398fc786b88cc846ec89af85a51a')
APP = b'snow-wasm-agent-application'
REPLY = b'python-brain-application'

def read_frame():
    line = sys.stdin.readline()
    if not line: raise EOFError
    return bytes.fromhex(json.loads(line)['frame'])
def write_frame(frame):
    print(json.dumps({'frame': bytes(frame).hex()}, separators=(',', ':')), flush=True)

def stdio(role, binding):
    if role == 'responder':
        s = Session(BRAIN_PRIVATE, AGENT_PUBLIC, False, binding)
        s.handshake_read(read_frame()); write_frame(s.handshake_write())
        if s.unseal(read_frame(), True) != b'client-ready': raise RuntimeError('bad client confirmation')
        write_frame(s.seal(b'server-ready', True)); s.open = True
        if s.unseal(read_frame()) != APP: raise RuntimeError('bad app payload')
        write_frame(s.seal(REPLY)); s.close()
    else:
        s = Session(AGENT_PRIVATE, BRAIN_PUBLIC, True, binding)
        write_frame(s.handshake_write()); s.handshake_read(read_frame())
        write_frame(s.seal(b'client-ready', True))
        if s.unseal(read_frame(), True) != b'server-ready': raise RuntimeError('bad server confirmation')
        s.open = True; write_frame(s.seal(APP))
        if s.unseal(read_frame()) != REPLY: raise RuntimeError('bad app reply')
        s.close()

async def websocket_server(binding):
    from websockets.asyncio.server import serve
    async def handler(ws):
        if getattr(ws, 'request', None) is not None and ws.request.path != '/session-spike':
            await ws.close(code=1008, reason='path'); return
        s = Session(BRAIN_PRIVATE, AGENT_PUBLIC, False, binding)
        try:
            await establish(ws, s, False, timeout=2)
            if s.unseal(await ws.recv()) != APP: raise RuntimeError('bad app payload')
            await ws.send(s.seal(REPLY))
        finally: s.close()
    async with serve(handler, '127.0.0.1', 17871, max_size=36864, max_queue=1, compression=None):
        print('ready', flush=True)
        await asyncio.Future()

def main():
    p = argparse.ArgumentParser(); p.add_argument('--binding', required=True); p.add_argument('--role', choices=['initiator','responder']); p.add_argument('--websocket', action='store_true')
    a = p.parse_args(); binding = bytes.fromhex(a.binding)
    if a.websocket: asyncio.run(websocket_server(binding))
    elif a.role: stdio(a.role, binding)
    else: p.error('choose --role or --websocket')
if __name__ == '__main__': main()
