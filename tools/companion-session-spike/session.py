"""Research-only Noise KK session over an untrusted ordered binary channel.

Pins are supplied by the test harness, never learned from the channel. This
module implements framing and admission around Noise, not cryptographic math.
"""

import asyncio
from noise.connection import Keypair, NoiseConnection

PROFILE = b"Noise_KK_25519_ChaChaPoly_SHA256"
PROLOGUE = b"ofca-session-spike/v1;agent-to-brain;no-early-data"
MAX_FRAME = 4096


class SessionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Session:
    def __init__(self, private, peer_pin, initiator, binding=b"synthetic-pairing-v1"):
        if len(private) != 32 or len(peer_pin) != 32 or not binding or len(binding) > 256:
            raise SessionError("invalid_pin")
        self.noise = NoiseConnection.from_name(PROFILE)
        self.noise.set_as_initiator() if initiator else self.noise.set_as_responder()
        self.noise.set_keypair_from_private_bytes(Keypair.STATIC, private)
        self.noise.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, peer_pin)
        self.noise.set_prologue(PROLOGUE + b"\x00" + binding)
        self.noise.start_handshake()
        self.open = False
        self.failed = False

    def close(self):
        self.open = False
        self.failed = True
        self.noise = None

    def refuse(self, code):
        self.close()
        raise SessionError(code) from None

    def frame(self, frame):
        if self.failed:
            raise SessionError("session_closed")
        if not isinstance(frame, (bytes, bytearray)) or len(frame) > MAX_FRAME:
            self.refuse("invalid_frame")
        return bytes(frame)

    def handshake_write(self):
        try:
            return bytes(self.noise.write_message(b""))
        except Exception:
            self.refuse("handshake_failed")

    def handshake_read(self, frame):
        frame = self.frame(frame)
        try:
            if self.noise.read_message(frame):
                self.refuse("early_data_refused")
        except Exception:
            self.refuse("handshake_failed")

    def seal(self, payload, control=False):
        if self.failed or (not control and not self.open):
            self.refuse("session_not_ready")
        if not isinstance(payload, bytes) or len(payload) > MAX_FRAME - 17:
            self.refuse("payload_too_large")
        try:
            return self.noise.encrypt((b"\x00" if control else b"\x01") + payload)
        except Exception:
            self.refuse("encryption_failed")

    def unseal(self, frame, control=False):
        frame = self.frame(frame)
        if not control and not self.open:
            self.refuse("session_not_ready")
        try:
            payload = self.noise.decrypt(frame)
        except Exception:
            self.refuse("authentication_failed")
        if payload[:1] != (b"\x00" if control else b"\x01"):
            self.refuse("unexpected_record")
        return bytes(payload[1:])


async def establish(ws, session, initiator, timeout=2):
    """One total handshake/confirmation deadline; no application early data."""
    try:
        async with asyncio.timeout(timeout):
            if initiator:
                await ws.send(session.handshake_write())
                session.handshake_read(await ws.recv())
                await ws.send(session.seal(b"client-ready", control=True))
                if session.unseal(await ws.recv(), control=True) != b"server-ready":
                    session.refuse("confirmation_failed")
            else:
                session.handshake_read(await ws.recv())
                await ws.send(session.handshake_write())
                if session.unseal(await ws.recv(), control=True) != b"client-ready":
                    session.refuse("confirmation_failed")
                await ws.send(session.seal(b"server-ready", control=True))
            session.open = True
    except asyncio.CancelledError:
        session.close()
        raise
    except TimeoutError:
        session.refuse("deadline_exceeded")
    except SessionError:
        raise
    except Exception:
        session.refuse("transport_failed")


async def exchange(uri, private, pin, payload, timeout=2):
    """Initiator probe: connection, handshake, and one response share a deadline."""
    from websockets.asyncio.client import connect
    if uri != "ws://127.0.0.1:17871/session-spike":
        raise SessionError("endpoint_refused")
    session = Session(private, pin, True)
    try:
        async with asyncio.timeout(timeout):
            async with connect(uri, max_size=MAX_FRAME, max_queue=1,
                               compression=None, proxy=None, close_timeout=0.1) as ws:
                await establish(ws, session, True, timeout=timeout)
                await ws.send(session.seal(payload))
                return session.unseal(await ws.recv())
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise SessionError("deadline_exceeded") from None
    except SessionError:
        raise
    except Exception:
        raise SessionError("transport_failed") from None
    finally:
        session.close()
