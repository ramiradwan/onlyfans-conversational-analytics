from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import ofca_native_snow
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

HOST = "127.0.0.1"
PORT = 17871
PATH = "/native-snow-spike"
PAIRING_DIGEST = bytes.fromhex("da282a55e299bcd3893680db428f699bff8e340a8f87e5cb20e9808b1dc1c62f")
AGENT_PUBLIC = bytes.fromhex("d89e3bad79437dbed9f843418304f460ff05c7fe81fe4a9577a804cb9367ff66")
BRAIN_PRIVATE = bytes.fromhex("404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f")
PROFILE_PREFIX = b"ofca-companion-session/v1;agent-to-brain;no-early-data"
CLIENT_READY = b"\x00client-ready"
SERVER_READY = b"\x00server-ready"
APP = 1

EXPECTED_CASES = (
    "positive",
    "fresh-1",
    "fresh-2",
    "wrong-brain-key",
    "wrong-agent-key",
    "wrong-prologue",
    "altered-digest",
    "tampered-handshake",
    "replay-capture",
    "replay",
    "reordered",
    "tampered-transport",
    "replayed-transport",
    "oversize-ordinary",
    "oversize-authorization",
    "malformed-frame",
    "cancel",
    "deadline",
)


class ExpectedRefusal(Exception):
    pass


class UnexpectedSuccess(Exception):
    pass


def profile_path() -> Path:
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS"))
    else:
        root = Path(__file__).resolve().parents[2]
    return root / "contracts" / "companion-pairing-profile" / "profile.json"


def load_limits() -> tuple[int, int, int]:
    profile = json.loads(profile_path().read_text(encoding="utf-8"))
    limits = profile["limits"]
    return (
        int(limits["max_application_record_plaintext_bytes"]),
        int(limits["max_authorization_record_plaintext_bytes"]),
        int(limits["max_grant_characters"]),
    )


ORDINARY_LIMIT, AUTH_LIMIT, GRANT_LIMIT = load_limits()
assert ORDINARY_LIMIT == 4079
assert AUTH_LIMIT == 36847
assert GRANT_LIMIT == 16384


def prologue(digest: bytes = PAIRING_DIGEST) -> bytes:
    return PROFILE_PREFIX + b"\x00" + digest


def flip_first(value: bytes) -> bytes:
    out = bytearray(value)
    # X25519 clamps the low three scalar bits, so flip bit 3 rather than bit 0.
    # This guarantees the wrong-private-key case actually changes the scalar.
    out[0] ^= 0x08
    return bytes(out)


def max_authorization_payload() -> bytes:
    middle = GRANT_LIMIT - 4
    installation = "a." + ("b" * middle) + ".c"
    binding = "d." + ("e" * middle) + ".f"
    assert len(installation) == GRANT_LIMIT == len(binding)
    encoded = json.dumps(
        {
            "type": "session.authorization",
            "installation_grant": installation,
            "creator_account_binding": binding,
        },
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > AUTH_LIMIT:
        raise RuntimeError("contract_authorization_limit_too_small")
    return encoded + (b" " * (AUTH_LIMIT - len(encoded)))


AUTH_PAYLOAD = max_authorization_payload()
assert len(AUTH_PAYLOAD) == AUTH_LIMIT


async def recv_binary(ws: ServerConnection, timeout: float = 2.0) -> bytes:
    try:
        async with asyncio.timeout(timeout):
            value = await ws.recv()
    except TimeoutError as exc:
        raise ExpectedRefusal("deadline") from exc
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ExpectedRefusal("malformed_frame")
    return bytes(value)


@dataclass
class CaseResult:
    result: str
    reason: str | None = None
    handshake_hash: str | None = None
    first_handshake_sha256: str | None = None
    responder_handshake_sha256: str | None = None
    closed: bool = True


@dataclass
class QualificationState:
    results: dict[str, CaseResult] = field(default_factory=dict)
    complete: asyncio.Event = field(default_factory=asyncio.Event)

    def record(self, name: str, result: CaseResult) -> None:
        if name in self.results:
            self.results[name] = CaseResult("failed", "duplicate_case")
        else:
            self.results[name] = result
        if all(name in self.results for name in EXPECTED_CASES):
            self.complete.set()


async def handshake(
    ws: ServerConnection,
    responder: ofca_native_snow.NoiseResponder,
    *,
    first_timeout: float = 2.0,
) -> tuple[bytes, bytes, str]:
    first = await recv_binary(ws, first_timeout)
    responder.consume_first_handshake(first)
    response = bytes(responder.produce_responder_handshake())
    if not responder.handshake_complete():
        raise RuntimeError("native_handshake_not_complete")
    digest = bytes(responder.handshake_hash()).hex()
    responder.enter_transport()
    await ws.send(response)
    return first, response, digest


async def confirmations(ws: ServerConnection, responder: ofca_native_snow.NoiseResponder) -> None:
    encrypted = await recv_binary(ws)
    plain = bytes(responder.decrypt_transport(encrypted))
    if plain != CLIENT_READY:
        raise ExpectedRefusal("client_confirmation")
    await ws.send(bytes(responder.encrypt_transport(SERVER_READY)))


async def send_authorization(ws: ServerConnection, responder: ofca_native_snow.NoiseResponder) -> None:
    record = bytes([APP]) + AUTH_PAYLOAD
    assert len(record) == AUTH_LIMIT + 1
    await ws.send(bytes(responder.encrypt_transport(record)))


async def positive_exchange(
    ws: ServerConnection,
    responder: ofca_native_snow.NoiseResponder,
) -> tuple[bytes, bytes, str]:
    first, response, digest = await handshake(ws, responder)
    await confirmations(ws, responder)
    await send_authorization(ws, responder)
    frame = await recv_binary(ws)
    plain = bytes(responder.decrypt_transport(frame))
    if len(plain) != ORDINARY_LIMIT + 1 or plain[:1] != bytes([APP]):
        raise RuntimeError("ordinary_agent_record_mismatch")
    reply = bytes([APP]) + (b"B" * ORDINARY_LIMIT)
    await ws.send(bytes(responder.encrypt_transport(reply)))
    return first, response, digest


def make_responder(case: str) -> ofca_native_snow.NoiseResponder:
    private = BRAIN_PRIVATE
    peer = AGENT_PUBLIC
    binding = prologue()
    if case == "wrong-brain-key":
        private = flip_first(private)
    elif case == "wrong-agent-key":
        peer = flip_first(peer)
    elif case == "wrong-prologue":
        binding = flip_first(binding)
    elif case == "altered-digest":
        altered = bytearray(PAIRING_DIGEST)
        altered[-1] ^= 1
        binding = prologue(bytes(altered))
    return ofca_native_snow.NoiseResponder(private, peer, binding)


async def run_case(case: str, ws: ServerConnection) -> CaseResult:
    responder: ofca_native_snow.NoiseResponder | None = None
    try:
        if case == "wrong-prologue":
            try:
                make_responder(case)
            except ofca_native_snow.NoiseInputError:
                raise ExpectedRefusal("invalid_prologue") from None
            raise UnexpectedSuccess("wrong_prologue_constructed")

        responder = make_responder(case)

        if case in {"positive", "fresh-1", "fresh-2", "replay-capture"}:
            first, response, digest = await positive_exchange(ws, responder)
            import hashlib
            return CaseResult(
                "passed",
                handshake_hash=digest,
                first_handshake_sha256=hashlib.sha256(first).hexdigest(),
                responder_handshake_sha256=hashlib.sha256(response).hexdigest(),
            )

        if case == "wrong-brain-key":
            try:
                await handshake(ws, responder)
            except (ofca_native_snow.NoiseError, ExpectedRefusal, ConnectionClosed):
                raise ExpectedRefusal("wrong_brain_key_refused") from None
            try:
                frame = await recv_binary(ws)
            except (ExpectedRefusal, ConnectionClosed):
                raise ExpectedRefusal("agent_refused_wrong_brain") from None
            try:
                responder.decrypt_transport(frame)
            except ofca_native_snow.NoiseError:
                raise ExpectedRefusal("agent_refused_wrong_brain") from None
            raise UnexpectedSuccess("wrong_brain_authenticated")

        if case in {"wrong-agent-key", "altered-digest", "tampered-handshake", "reordered"}:
            try:
                await handshake(ws, responder)
            except (ofca_native_snow.NoiseError, ExpectedRefusal, ConnectionClosed):
                raise ExpectedRefusal(case) from None
            raise UnexpectedSuccess(f"{case}_accepted")

        if case == "replay":
            await handshake(ws, responder)
            try:
                await confirmations(ws, responder)
            except (ofca_native_snow.NoiseError, ExpectedRefusal, ConnectionClosed):
                raise ExpectedRefusal("replayed_handshake_not_admitted") from None
            raise UnexpectedSuccess("replay_confirmed")

        if case in {"tampered-transport", "replayed-transport", "oversize-ordinary"}:
            await handshake(ws, responder)
            await confirmations(ws, responder)
            await send_authorization(ws, responder)
            if case == "oversize-ordinary":
                try:
                    await recv_binary(ws, 1.0)
                except (ExpectedRefusal, ConnectionClosed):
                    raise ExpectedRefusal("oversize_ordinary_refused") from None
                raise UnexpectedSuccess("oversize_ordinary_sent")
            first_app = await recv_binary(ws)
            try:
                plain = bytes(responder.decrypt_transport(first_app))
            except ofca_native_snow.NoiseError:
                if case == "tampered-transport":
                    raise ExpectedRefusal("tampered_transport") from None
                raise
            if case == "tampered-transport":
                raise UnexpectedSuccess("tampered_transport_accepted")
            if plain[:1] != bytes([APP]):
                raise UnexpectedSuccess("first_replay_record_invalid")
            await ws.send(bytes(responder.encrypt_transport(bytes([APP]) + b"ack")))
            replayed = await recv_binary(ws)
            try:
                responder.decrypt_transport(replayed)
            except ofca_native_snow.NoiseError:
                raise ExpectedRefusal("replayed_transport") from None
            raise UnexpectedSuccess("replayed_transport_accepted")

        if case == "oversize-authorization":
            await handshake(ws, responder)
            await confirmations(ws, responder)
            oversized = bytes([APP]) + AUTH_PAYLOAD + b" "
            assert len(oversized) == AUTH_LIMIT + 2
            try:
                responder.encrypt_transport(oversized)
            except ofca_native_snow.NoiseInputError:
                raise ExpectedRefusal("oversize_authorization") from None
            raise UnexpectedSuccess("oversize_authorization_encrypted")

        if case == "malformed-frame":
            try:
                await handshake(ws, responder)
            except ExpectedRefusal:
                raise ExpectedRefusal("malformed_frame") from None
            raise UnexpectedSuccess("malformed_frame_accepted")

        if case == "cancel":
            try:
                await handshake(ws, responder)
                await recv_binary(ws, 1.0)
            except (ExpectedRefusal, ConnectionClosed, ofca_native_snow.NoiseError):
                raise ExpectedRefusal("cancelled") from None
            raise UnexpectedSuccess("cancel_not_observed")

        if case == "deadline":
            try:
                await handshake(ws, responder, first_timeout=0.05)
            except ExpectedRefusal:
                raise ExpectedRefusal("deadline") from None
            raise UnexpectedSuccess("deadline_not_enforced")

        raise RuntimeError("unknown_case")
    except ExpectedRefusal as refused:
        return CaseResult("passed", str(refused), closed=True)
    finally:
        if responder is not None:
            responder.close()


async def main_async(report_path: Path) -> int:
    state = QualificationState()

    async def handler(ws: ServerConnection) -> None:
        parsed = urlsplit(ws.request.path)
        if parsed.path != PATH:
            await ws.close(code=1008)
            return
        case = parse_qs(parsed.query).get("case", [""])[0]
        if case not in EXPECTED_CASES:
            await ws.close(code=1008)
            return
        try:
            result = await run_case(case, ws)
        except Exception as exc:
            result = CaseResult("failed", type(exc).__name__, closed=True)
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        state.record(case, result)

    async with serve(
        handler,
        HOST,
        PORT,
        max_size=AUTH_LIMIT + 128,
        max_queue=1,
        compression=None,
        ping_interval=None,
        close_timeout=0.1,
    ):
        print("ready", flush=True)
        try:
            async with asyncio.timeout(90):
                await state.complete.wait()
        except TimeoutError:
            pass

    missing = [case for case in EXPECTED_CASES if case not in state.results]
    report = {
        "suite": ofca_native_snow.SUITE,
        "ordinary_plaintext_limit": ORDINARY_LIMIT,
        "authorization_plaintext_limit": AUTH_LIMIT,
        "cases": {name: vars(result) for name, result in state.results.items()},
        "missing": missing,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if not missing and all(r.result == "passed" for r in state.results.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(main_async(args.report.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
