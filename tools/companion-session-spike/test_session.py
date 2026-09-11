"""Synthetic evidence only; no production keys, grants, or browser claims."""
import asyncio
import subprocess
import sys
from pathlib import Path
import unittest

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization
from websockets.asyncio.server import serve

from session import (Session, SessionError, establish, exchange, MAX_FRAME,
                     MAX_APPLICATION_PAYLOAD, MAX_AUTHORIZATION_PAYLOAD)

URI = "ws://127.0.0.1:17871/session-spike"
SENSITIVE = b"synthetic-message|storage-key|auth-ticket|rotation-material"


def keypair():
    key = X25519PrivateKey.generate()
    return (key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption()),
            key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def pair(binding=b"synthetic-pairing-v1"):
    a, ap = keypair()
    b, bp = keypair()
    return Session(a, bp, True, binding), Session(b, ap, False, binding)


def ready(a, b):
    first = a.handshake_write()
    b.handshake_read(first)
    second = b.handshake_write()
    a.handshake_read(second)
    assert b.unseal(a.seal(b"client-ready", True), True) == b"client-ready"
    assert a.unseal(b.seal(b"server-ready", True), True) == b"server-ready"
    a.open = b.open = True
    return first, second


class Records(unittest.TestCase):
    def test_authorization_record_carries_the_published_bound(self):
        a, b = pair()
        ready(a, b)
        payload = b"a" * MAX_AUTHORIZATION_PAYLOAD
        frame = b.seal(payload, authorization=True)
        self.assertEqual(len(frame), MAX_FRAME)
        self.assertEqual(a.unseal(frame), payload)
        with self.assertRaises(SessionError) as caught:
            b.seal(payload, authorization=False)
        self.assertEqual(caught.exception.code, "payload_too_large")
        c, d = pair()
        ready(c, d)
        with self.assertRaises(SessionError) as caught:
            d.seal(payload + b"a", authorization=True)
        self.assertEqual(caught.exception.code, "payload_too_large")
        self.assertEqual(MAX_APPLICATION_PAYLOAD, 4079)

    def test_round_trip_and_no_cleartext(self):
        a, b = pair()
        transcript = ready(a, b)
        encrypted = a.seal(SENSITIVE)
        self.assertNotIn(SENSITIVE, b"".join(transcript) + encrypted)
        self.assertEqual(b.unseal(encrypted), SENSITIVE)
        self.assertEqual(a.unseal(b.seal(b"reply")), b"reply")

    def test_no_application_data_before_confirmation(self):
        a, _ = pair()
        with self.assertRaisesRegex(SessionError, "session_not_ready"):
            a.seal(SENSITIVE)

    def test_tamper_replay_reorder_and_cross_session_close_permanently(self):
        for attack in ("tamper", "replay", "reorder", "cross-session", "reflection"):
            with self.subTest(attack=attack):
                a, b = pair()
                ready(a, b)
                first = a.seal(SENSITIVE)
                if attack == "tamper":
                    frame = first[:-1] + bytes([first[-1] ^ 1])
                elif attack == "replay":
                    b.unseal(first)
                    frame = first
                elif attack == "reorder":
                    frame = a.seal(b"second")
                elif attack == "reflection":
                    frame = b.seal(b"wrong-direction")
                else:
                    x, y = pair()
                    ready(x, y)
                    frame = x.seal(SENSITIVE)
                with self.assertRaisesRegex(SessionError, "authentication_failed"):
                    b.unseal(frame)
                self.assertTrue(b.failed)
                with self.assertRaises(SessionError):
                    b.unseal(first)

    def test_peer_pin_and_profile_mismatch(self):
        a, _ = pair()
        _, wrong = pair()
        with self.assertRaises(SessionError):
            wrong.handshake_read(a.handshake_write())
        ka, pa = keypair()
        kb, pb = keypair()
        a = Session(ka, pb, True, b"profile-v1")
        b = Session(kb, pa, False, b"profile-v0")
        with self.assertRaises(SessionError):
            b.handshake_read(a.handshake_write())

    def test_size_and_text_refusals_are_payload_free(self):
        for frame in (b"x" * (MAX_FRAME + 1), "sensitive plaintext"):
            a, _ = pair()
            with self.assertRaises(SessionError) as result:
                a.handshake_read(frame)
            self.assertEqual(str(result.exception), "invalid_frame")


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_exchange_on_existing_loopback_port(self):
        a, ap = keypair()
        b, bp = keypair()
        captured = []
        async def handler(ws):
            session = Session(b, ap, False)
            try:
                await establish(ws, session, False)
                encrypted = await ws.recv()
                captured.append(encrypted)
                self.assertEqual(session.unseal(encrypted), SENSITIVE)
                await ws.send(session.seal(b"accepted"))
            finally:
                session.close()
        async with serve(handler, "127.0.0.1", 17871, max_size=MAX_FRAME,
                         max_queue=1, compression=None):
            self.assertEqual(await exchange(URI, a, bp, SENSITIVE), b"accepted")
        self.assertTrue(captured)
        self.assertNotIn(SENSITIVE, b"".join(captured))

    async def test_deadline_and_cancellation(self):
        a, _ = keypair()
        _, bp = keypair()
        async def stall(ws):
            await ws.wait_closed()
        async with serve(stall, "127.0.0.1", 17871, max_size=MAX_FRAME, compression=None):
            with self.assertRaisesRegex(SessionError, "deadline_exceeded"):
                await exchange(URI, a, bp, SENSITIVE, timeout=0.1)
            task = asyncio.create_task(exchange(URI, a, bp, SENSITIVE))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_hostile_process_occupies_17871(self):
        a, _ = keypair()
        _, bp = keypair()
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("hostile.py")),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual((await asyncio.wait_for(process.stdout.readline(), 3)).strip(), b"ready")
            with self.assertRaises(SessionError):
                await exchange(URI, a, bp, SENSITIVE)
            result = await asyncio.wait_for(process.stdout.readline(), 3)
            self.assertEqual(result.strip(), b"application-secret-observed=false")
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
