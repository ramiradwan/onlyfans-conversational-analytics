from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.security import AuthContext, csrf_token, get_auth_context
from app.core.config import settings
from app.main import app
from app.persistence.history import StreamKey
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from app.transport.manager import (
    DEV_ACCOUNT_ID,
    DEV_AGENT_AUTH_TICKET,
    DEV_BRIDGE_AUTH_TICKET,
    DEV_PRINCIPAL_ID,
    AgentLease,
    transport_manager,
)


INSTALLATION_ID = UUID("20000000-0000-4000-8000-000000000001")
STREAM_ID = UUID("30000000-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
def reset_manager():
    transport_manager.reset()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def envelope(message_type: str, payload: dict):
    return AGENT_TO_BRAIN_ADAPTER.validate_json(
        json.dumps(
            {
                "type": message_type,
                "protocol_version": "2",
                "message_id": str(uuid4()),
                "payload": payload,
            }
        )
    )


def snapshot_identity(snapshot_id: UUID) -> dict:
    return {
        "connection_id": "10000000-0000-4000-8000-000000000001",
        "fencing_token": "fence-test",
        "creator_account_id": DEV_ACCOUNT_ID,
        "agent_installation_id": str(INSTALLATION_ID),
        "agent_stream_id": str(STREAM_ID),
        "snapshot_id": str(snapshot_id),
    }


def seed_projection() -> None:
    history = transport_manager.history
    key = StreamKey(DEV_ACCOUNT_ID, INSTALLATION_ID, STREAM_ID)
    snapshot_id = uuid4()
    begin = envelope(
        "ingest.snapshot",
        {
            **snapshot_identity(snapshot_id),
            "frame_kind": "begin",
            "through_seq": 0,
            "chunk_count": 2,
            "record_counts": {"chats": 2, "messages": 5, "coverage_evidence": 0},
            "max_frame_bytes": 524288,
        },
    ).payload
    chats = envelope(
        "ingest.snapshot",
        {
            **snapshot_identity(snapshot_id),
            "frame_kind": "chunk",
            "chunk_index": 0,
            "entity_kind": "chat",
            "records": [
                {
                    "tombstone": False,
                    "chat": {
                        "record_kind": "full",
                        "chat_id": chat_id,
                        "platform_user_id": f"fan-{chat_id}",
                        "display_name": chat_id,
                        "updated_at": "2026-07-19T10:00:00Z",
                    },
                }
                for chat_id in ("chat-1", "chat-2")
            ],
        },
    ).payload
    messages = envelope(
        "ingest.snapshot",
        {
            **snapshot_identity(snapshot_id),
            "frame_kind": "chunk",
            "chunk_index": 1,
            "entity_kind": "message",
            "records": [
                {
                    "tombstone": False,
                    "message": {
                        "message_id": f"message-{index}",
                        "chat_id": "chat-1",
                        "sender_platform_user_id": "fan-chat-1",
                        "text": f"Message {index}",
                        "sent_at": "2026-07-19T10:00:00Z",
                        "direction": "inbound",
                    },
                }
                for index in range(1, 6)
            ],
        },
    ).payload
    commit = envelope(
        "ingest.snapshot",
        {
            **snapshot_identity(snapshot_id),
            "frame_kind": "commit",
            "chunk_count": 2,
        },
    ).payload
    assert history.begin_snapshot(key, begin).status == "accepted"
    assert history.add_snapshot_chunk(key, chats).status == "accepted"
    assert history.add_snapshot_chunk(key, messages).status == "accepted"
    assert history.commit_snapshot(key, commit).status == "accepted"
    assert transport_manager.projection.catch_up(DEV_ACCOUNT_ID) is not None


def agent_hello(ticket: str) -> dict:
    return {
        "type": "agent.hello",
        "protocol_version": "2",
        "message_id": str(uuid4()),
        "payload": {
            "auth_ticket": ticket,
            "agent_installation_id": str(INSTALLATION_ID),
            "requested_creator_account_id": DEV_ACCOUNT_ID,
            "capabilities": ["capture.chats", "capture.messages", "history.sync"],
            "extension_version": "2.0.0-test",
            "agent_stream_id": str(STREAM_ID),
            "last_acknowledged_source_seq": 0,
            "applied_config_revision": None,
        },
    }


def creator_csrf() -> str:
    return csrf_token(
        AuthContext(
            DEV_PRINCIPAL_ID,
            DEV_ACCOUNT_ID,
            "creator",
            "dev-platform-creator",
            "development-session",
        )
    )


def test_runtime_exposes_only_bridge_ticket_and_pairing_is_single_use() -> None:
    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert root.headers["cache-control"] == "no-store"
        assert "BRIDGE_AUTH_TICKET" in root.text
        assert '"BRIDGE_ROLE": "creator"' in root.text
        assert DEV_BRIDGE_AUTH_TICKET not in root.text
        assert "local-v2." in root.text
        assert "AGENT_AUTH_TICKET" not in root.text
        assert DEV_AGENT_AUTH_TICKET not in root.text

        pairing = client.post(
            "/api/v1/agent/pairing", headers={"X-CSRF-Token": creator_csrf()}
        )
        assert pairing.status_code == 200
        assert pairing.headers["cache-control"] == "no-store"
        pairing_ticket = pairing.json()["pairing_ticket"]

        with client.websocket_connect("/ws/agent") as agent:
            agent.send_json(agent_hello(pairing_ticket))
            session = agent.receive_json()
            assert session["type"] == "agent.session"
            config_ticket = session["payload"]["config_auth_ticket"]
            assert config_ticket != pairing_ticket
            assert pairing_ticket not in json.dumps(session)
            assert agent.receive_json()["type"] == "sync.required"

        config = client.get(
            "/api/v1/agent/config",
            headers={"Authorization": f"Bearer {config_ticket}"},
            params={
                "protocol_version": "2",
                "agent_installation_id": str(INSTALLATION_ID),
                "creator_account_id": DEV_ACCOUNT_ID,
                "supported_config_schema_versions": "2",
            },
        )
        assert config.status_code == 200
        wrong_installation = client.get(
            "/api/v1/agent/config",
            headers={"Authorization": f"Bearer {config_ticket}"},
            params={
                "protocol_version": "2",
                "agent_installation_id": str(uuid4()),
                "creator_account_id": DEV_ACCOUNT_ID,
                "supported_config_schema_versions": "2",
            },
        )
        assert wrong_installation.status_code == 401
        query_credential = client.get(
            "/api/v1/agent/config",
            headers={"Authorization": f"Bearer {config_ticket}"},
            params={
                "auth_ticket": config_ticket,
                "protocol_version": "2",
                "agent_installation_id": str(INSTALLATION_ID),
                "creator_account_id": DEV_ACCOUNT_ID,
                "supported_config_schema_versions": "2",
            },
        )
        assert query_credential.status_code == 400
        assert "must not appear in the URL" in query_credential.json()["detail"]

        with client.websocket_connect("/ws/agent") as replay:
            replay.send_json(agent_hello(pairing_ticket))
            rejected = replay.receive_json()
            assert rejected["type"] == "protocol.error"
            assert rejected["payload"]["code"] == "unauthorized"


def test_settings_are_csrf_cas_and_matching_config_revision_bound() -> None:
    with TestClient(app) as client:
        initial = client.get("/api/v1/settings/history")
        assert initial.status_code == 200
        assert initial.headers["cache-control"] == "no-store"
        assert initial.headers["etag"] == '"0"'
        assert initial.json()["settings_revision"] == 0
        request = {
            "desired_state": "running",
            "consent_policy_version": "history-consent-v1",
            "accept_consent": True,
            "recent_window_days": 30,
            "page_size": 100,
            "pages_per_wake": 2,
            "request_interval_ms": 500,
            "retry_limit": 3,
        }
        missing_csrf = client.put(
            "/api/v1/settings/history",
            headers={"If-Match": "0"},
            json=request,
        )
        assert missing_csrf.status_code == 403
        updated = client.put(
            "/api/v1/settings/history",
            headers={"If-Match": "0", "X-CSRF-Token": creator_csrf()},
            json=request,
        )
        assert updated.status_code == 200
        assert updated.headers["cache-control"] == "no-store"
        assert updated.headers["etag"] == '"1"'
        assert updated.json()["desired_state"] == "running"
        assert updated.json()["effective_state"] == "not_applied"
        internal = transport_manager.history.history_settings(DEV_ACCOUNT_ID)
        required_config = internal["required_config_revision"]
        assert required_config == "config-9"

        transport_manager.history.mark_history_config_applied(
            DEV_ACCOUNT_ID, "config-8"
        )
        assert transport_manager.history.history_settings(DEV_ACCOUNT_ID)[
            "effective_settings_revision"
        ] is None
        transport_manager.history.mark_history_config_applied(
            DEV_ACCOUNT_ID, required_config
        )
        effective = transport_manager.history.history_settings(DEV_ACCOUNT_ID)
        assert effective["effective_state"] == "running"
        assert effective["effective_settings_revision"] == 1

        stale = client.put(
            "/api/v1/settings/history",
            headers={"If-Match": "0", "X-CSRF-Token": creator_csrf()},
            json={**request, "desired_state": "paused", "accept_consent": False, "consent_policy_version": None},
        )
        assert stale.status_code == 412
        assert transport_manager.history.history_settings(DEV_ACCOUNT_ID)[
            "settings_revision"
        ] == 1

        assert client.delete(
            "/api/v1/settings/history/revoke",
            headers={"If-Match": "1", "X-CSRF-Token": creator_csrf()},
        ).status_code in {404, 405}
        revoked = client.delete(
            "/api/v1/settings/history/consent",
            headers={"If-Match": "1", "X-CSRF-Token": creator_csrf()},
        )
        assert revoked.status_code == 200
        assert revoked.json()["desired_state"] == "revoked"

        app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            "operator-1", DEV_ACCOUNT_ID, "operator"
        )
        assert client.get("/api/v1/settings/history").status_code == 200
        denied = client.put(
            "/api/v1/settings/history",
            headers={
                "If-Match": "1",
                "X-CSRF-Token": csrf_token(
                    AuthContext("operator-1", DEV_ACCOUNT_ID, "operator")
                ),
            },
            json={**request, "desired_state": "paused", "accept_consent": False, "consent_policy_version": None},
        )
        assert denied.status_code == 403


def test_history_config_ack_is_reconciled_after_bind_and_on_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = DEV_ACCOUNT_ID
    settings_record = transport_manager.history.update_history_settings(
        account_id,
        expected_revision=0,
        values={
            "consent_policy_version": "history-consent-v1",
            "consent_revision": "consent-1",
            "authorized_platform_creator_id": "platform-creator-1",
            "desired_state": "running",
            "recent_window_days": 30,
            "page_size": 100,
            "pages_per_wake": 2,
            "request_interval_ms": 500,
            "retry_limit": 3,
        },
    )
    sent = asyncio.Event()

    async def signal(account: str) -> bool:
        assert account == account_id
        sent.set()
        return True

    monkeypatch.setattr(transport_manager, "signal_config_available", signal)

    async def exercise() -> None:
        now = datetime(2026, 7, 19, tzinfo=timezone.utc)
        lease = AgentLease(
            websocket=None,
            principal_id=DEV_PRINCIPAL_ID,
            creator_account_id=account_id,
            connection_id=UUID("00000000-0000-4000-8000-000000000098"),
            fencing_token="test-fence",
            agent_installation_id=UUID("00000000-0000-4000-8000-000000000099"),
            agent_stream_id=UUID("00000000-0000-4000-8000-000000000097"),
            config_auth_ticket="test-config-ticket",
            applied_config_revision="config-9",
            last_heartbeat_at=now,
        )
        transport_manager.active_agents[account_id] = lease
        try:
            document = await transport_manager.publish_history_settings(
                account_id, settings_record
            )
            assert sent.is_set()
            assert document.config_revision == "config-9"
            effective = transport_manager.history.history_settings(account_id)
            assert effective["effective_settings_revision"] == 1

            transport_manager.history.update_history_settings(
                account_id,
                expected_revision=1,
                values={
                    **settings_record,
                    "desired_state": "paused",
                },
            )
            current = transport_manager.required_config_document(account_id)
            heartbeat_document = await transport_manager.publish_config(
                account_id,
                capture_policy=current.capture_policy.model_dump(mode="json"),
                command_policy=current.command_policy.model_dump(mode="json"),
                history_acquisition={
                    "enabled": False,
                    "consent_revision": "consent-1",
                    "authorized_platform_creator_id": "platform-creator-1",
                    "recent_window_days": 30,
                    "page_size": 100,
                    "pages_per_wake": 2,
                    "request_interval_ms": 500,
                    "retry_limit": 3,
                },
                signal_available=False,
            )
            transport_manager.history.bind_history_config(
                account_id,
                settings_revision=2,
                config_revision=heartbeat_document.config_revision,
            )
            await transport_manager.heartbeat(
                lease, heartbeat_document.config_revision, now=now
            )
            effective = transport_manager.history.history_settings(account_id)
            assert effective["effective_settings_revision"] == 2
            assert effective["effective_state"] == "paused"
        finally:
            transport_manager.active_agents.pop(account_id, None)

    asyncio.run(exercise())


def test_unavailable_projection_metrics_are_null_and_legacy_sample_routes_are_absent() -> None:
    snapshot = transport_manager.projection.snapshot(DEV_ACCOUNT_ID)
    assert snapshot["projection"]["status"] == "unavailable"
    assert all(metric["value"] is None for metric in snapshot["analytics"].values())
    with TestClient(app) as client:
        assert client.get("/api/v1/insights/sentiment").status_code == 404
        assert client.get("/api/v1/schemas/wss").status_code == 404


def test_local_session_bootstrap_uses_header_once_and_sets_exact_secure_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = "bootstrap-" + uuid4().hex
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(settings, "local_session_bootstrap_token", SecretStr(bootstrap))
    monkeypatch.setattr(settings, "local_principal_id", "principal-bootstrap")
    monkeypatch.setattr(settings, "local_creator_account_id", "account-bootstrap")
    monkeypatch.setattr(settings, "local_platform_creator_id", "platform-bootstrap")
    monkeypatch.setattr(settings, "local_bridge_role", "operator")

    with TestClient(app, base_url="http://bridge.localhost:17871") as client:
        leaked = client.get(
            "/api/v1/session/bootstrap",
            params={"ticket": bootstrap},
            follow_redirects=False,
        )
        assert leaked.status_code != 200
        established = client.post(
            "/api/v1/session/bootstrap",
            headers={"Authorization": f"Bootstrap {bootstrap}"},
            follow_redirects=False,
        )
        assert established.status_code == 303
        assert established.headers["cache-control"] == "no-store"
        cookie_header = established.headers["set-cookie"]
        assert settings.bridge_session_cookie_name in cookie_header
        assert "HttpOnly" in cookie_header
        assert "Secure" in cookie_header
        assert "SameSite=strict" in cookie_header
        session_cookie = established.cookies[settings.bridge_session_cookie_name]

        page = client.get(
            "/",
            headers={
                "Cookie": f"{settings.bridge_session_cookie_name}={session_cookie}",
            },
        )
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert '"CREATOR_ID": "account-bootstrap"' in page.text
        assert '"BRIDGE_ROLE": "operator"' in page.text
        assert "platform-bootstrap" not in page.text

        replay = client.post(
            "/api/v1/session/bootstrap",
            headers={"Authorization": f"Bootstrap {bootstrap}"},
            follow_redirects=False,
        )
        assert replay.status_code == 401


def test_hmac_cursor_pages_ties_and_old_generation_returns_409() -> None:
    seed_projection()
    with TestClient(app) as client:
        first = client.get("/api/v1/conversations/chat-1/messages", params={"limit": 2})
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        first_page = first.json()
        assert [item["message_id"] for item in first_page["items"]] == [
            "message-4",
            "message-5",
        ]
        assert set(first_page["projection"]) == {
            "status", "canonical_revision", "projected_revision", "projected_at", "reason"
        }
        cursor = first_page["older_cursor"]
        assert cursor

        second = client.get(
            "/api/v1/conversations/chat-1/messages",
            params={"limit": 2, "before": cursor},
        )
        assert [item["message_id"] for item in second.json()["items"]] == [
            "message-2",
            "message-3",
        ]
        assert client.get(
            "/api/v1/conversations/chat-1/messages",
            params={"before": cursor[:-1] + ("A" if cursor[-1] != "A" else "B")},
        ).status_code == 400
        assert client.get(
            "/api/v1/conversations/chat-2/messages",
            params={"before": cursor},
        ).status_code == 400

        key = StreamKey(DEV_ACCOUNT_ID, INSTALLATION_ID, STREAM_ID)
        delta = envelope(
            "ingest.delta",
            {
                "connection_id": "10000000-0000-4000-8000-000000000001",
                "fencing_token": "fence-test",
                "creator_account_id": DEV_ACCOUNT_ID,
                "agent_installation_id": str(INSTALLATION_ID),
                "event_id": str(uuid4()),
                "agent_stream_id": str(STREAM_ID),
                "source_seq": 1,
                "acquisition_origin": "passive",
                "change": {
                    "type": "message.upsert",
                    "message": {
                        "message_id": "message-6",
                        "chat_id": "chat-1",
                        "sender_platform_user_id": "fan-chat-1",
                        "text": "Message 6",
                        "sent_at": "2026-07-19T10:01:00Z",
                        "direction": "inbound",
                    },
                },
            },
        ).payload
        assert transport_manager.history.commit_delta(key, delta).status == "accepted"
        transport_manager.projection.catch_up(DEV_ACCOUNT_ID)
        stale = client.get(
            "/api/v1/conversations/chat-1/messages",
            params={"limit": 2, "before": cursor},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"] == "cursor_stale"


def test_websocket_rejects_snapshot_frame_over_512_kib() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/agent") as agent:
        agent.send_json(agent_hello(DEV_AGENT_AUTH_TICKET))
        session = agent.receive_json()
        assert session["type"] == "agent.session"
        assert agent.receive_json()["type"] == "sync.required"
        payload = {
            "connection_id": session["payload"]["connection_id"],
            "fencing_token": session["payload"]["fencing_token"],
            "creator_account_id": DEV_ACCOUNT_ID,
            "agent_installation_id": str(INSTALLATION_ID),
            "agent_stream_id": str(STREAM_ID),
            "snapshot_id": str(uuid4()),
            "frame_kind": "chunk",
            "chunk_index": 0,
            "entity_kind": "message",
            "records": [
                {
                    "tombstone": False,
                    "message": {
                        "message_id": f"large-{index}",
                        "chat_id": "chat-1",
                        "sender_platform_user_id": "fan-chat-1",
                        "text": "x" * (300 * 1024),
                        "sent_at": "2026-07-19T10:00:00Z",
                        "direction": "inbound",
                    },
                }
                for index in range(2)
            ],
        }
        frame = {
            "type": "ingest.snapshot",
            "protocol_version": "2",
            "message_id": str(uuid4()),
            "payload": payload,
        }
        assert len(json.dumps(frame).encode("utf-8")) > 512 * 1024
        agent.send_json(frame)
        rejected = agent.receive_json()
        assert rejected["type"] == "ingest.rejected"
        assert rejected["payload"]["code"] == "frame_too_large"
