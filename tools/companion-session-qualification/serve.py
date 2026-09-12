"""Isolated contract-fixture host for the production pairing/session adapters.

This test tool is excluded from Brain packaging. Fixture grants use the pinned
contract clock; neither expiry validation nor trust checks are disabled in the
production modules. Control commands travel over stdin, never an HTTP endpoint.
"""

from __future__ import annotations

import json
import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

# Establish disposable test database paths and the reconstructable test-only
# wrapping key before importing any application module.
import conftest  # noqa: E402,F401
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.api.endpoints import companion_pairing, companion_session, transport_ws  # noqa: E402
from app.persistence.factory import create_canonical_repositories  # noqa: E402
from app.protocol import BRAIN_TO_AGENT_ADAPTER  # noqa: E402
from app.security.companion_noise import qualification_report  # noqa: E402
from app.security.companion_session_authority import CompanionSessionAuthority  # noqa: E402
from app.transport import companion_channel  # noqa: E402
from app.transport.manager import InMemoryTransportManager  # noqa: E402
from test_companion_pairing_proof import contract, grant_references  # noqa: E402
from test_companion_pairing_service import local  # noqa: E402


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix="ofca-production-peer-") as directory:
        root = Path(directory)
        fixture_contract = contract.__wrapped__()
        fixture_grants = grant_references.__wrapped__(fixture_contract)
        fixture = local.__wrapped__(root, fixture_contract, fixture_grants)
        patches = pytest.MonkeyPatch()
        patches.setattr(companion_pairing, "require_activated_runtime", lambda: None)
        patches.setattr(companion_pairing, "companion_pairing_service", lambda: fixture.service)
        patches.setattr(companion_pairing.settings, "websocket_bind_host", "127.0.0.1")
        patches.setattr(companion_pairing.settings, "auth_database_path", fixture.store.database.path)
        patches.setattr(companion_session, "session_authority", lambda: CompanionSessionAuthority(fixture.store))
        patches.setattr(companion_channel, "time", SimpleNamespace(time=lambda: fixture.clock.now.timestamp(), monotonic=time.monotonic))
        repositories = create_canonical_repositories(
            "sqlite", canonical_path=root / "canonical.sqlite3", projection_path=root / "projection.sqlite3"
        )
        manager = InMemoryTransportManager(repositories)
        manager.config_authority._authorized_accounts = lambda: frozenset({fixture.account})
        patches.setattr(companion_session, "transport_manager", manager)
        patches.setattr(transport_ws, "transport_manager", manager)
        app = FastAPI()
        event_loop = []

        @app.on_event("startup")
        async def capture_loop():
            event_loop.append(asyncio.get_running_loop())

        async def inbound_burst():
            lease = manager.active_agents[fixture.account]
            value = {
                "type": "command.execute", "protocol_version": "2",
                "message_id": "11111111-1111-4111-8111-111111111111",
                "payload": {
                    "connection_id": str(lease.connection_id), "fencing_token": lease.fencing_token,
                    "creator_account_id": fixture.account,
                    "command_id": "22222222-2222-4222-8222-222222222222",
                    "deadline": "2026-08-29T09:10:00Z", "idempotency_policy": "deduplicate",
                    "action": {"type": "message.send", "conversation_id": "qualification-chat", "text": "x", "media_url": None},
                },
            }
            encoded = json.dumps(value, separators=(",", ":"))
            value["payload"]["action"]["text"] = "x" * (524_288 - len(encoded.encode()) + 1)
            encoded = json.dumps(value, separators=(",", ":"))
            if len(encoded.encode()) != 524_288:
                raise RuntimeError("qualification_size_invalid")
            BRAIN_TO_AGENT_ADAPTER.validate_json(encoded)
            await lease.websocket.send_text(encoded)
            return {"sent": True}

        app.add_api_websocket_route("/ws/agent", companion_session.companion_session_socket)
        app.add_api_websocket_route("/ws/agent/pairing", companion_pairing.companion_pairing_socket)
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=17871, log_level="critical", access_log=False,
            ws_max_size=36_864, ws_max_queue=8, ws_per_message_deflate=False,
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("qualification_listener_unavailable")
            time.sleep(0.01)
        emit({"ready": True, "fixture_clock": fixture_contract["vector"]["now"],
              "account": fixture.account, "native": qualification_report()})
        pairing_id = None
        try:
            for line in sys.stdin:
                request = json.loads(line)
                operation = request.get("operation")
                if operation == "configure":
                    value = request["extension_id"]
                    if len(value) != 32 or any(character not in "abcdefghijklmnop" for character in value):
                        raise RuntimeError("qualification_extension_invalid")
                    patches.setattr(companion_pairing.settings, "extension_id", value)
                    result = {"configured": True}
                elif operation == "open":
                    result = fixture.service.open(fixture.policy, fixture.account)
                    pairing_id = result["pairing_id"]
                elif operation == "status":
                    result = fixture.service.status(fixture.policy, pairing_id)
                elif operation == "confirm":
                    status = fixture.service.status(fixture.policy, pairing_id)
                    if status["state"] != "awaiting_confirmation" or status["comparison_code"] != request["comparison_code"]:
                        raise RuntimeError("qualification_comparison_refused")
                    result = fixture.service.confirm(fixture.policy, pairing_id, status["version"])
                elif operation == "revoke":
                    status = fixture.service.status(fixture.policy, pairing_id)
                    result = fixture.service.revoke(fixture.policy, pairing_id, status["version"])
                elif operation == "inbound_burst":
                    result = asyncio.run_coroutine_threadsafe(inbound_burst(), event_loop[0]).result(timeout=10)
                elif operation == "stop":
                    emit({"stopped": True})
                    break
                else:
                    raise RuntimeError("qualification_command_invalid")
                emit(result)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            patches.undo()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never copy grant bytes, database records, or exception payloads into
        # CI output. The driver identifies the failing public operation.
        emit({"error": "production_peer_qualification_failed"})
        raise SystemExit(1) from None
