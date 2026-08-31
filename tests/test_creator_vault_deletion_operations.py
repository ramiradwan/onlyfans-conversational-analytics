from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.security import AuthContext, csrf_token, get_runtime_policy
from app.main import app
from app.security.runtime_policy import AuthorizationEpoch, RuntimePolicy
from app.transport.manager import DEV_ACCOUNT_ID, DEV_PRINCIPAL_ID, transport_manager

NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def policy() -> RuntimePolicy:
    return RuntimePolicy(
        identity=AuthContext(
            DEV_PRINCIPAL_ID,
            DEV_ACCOUNT_ID,
            "creator",
            "dev-platform-creator",
            "deletion-operation-session",
        ),
        authorization_epoch=AuthorizationEpoch(0),
    )


@pytest.fixture(autouse=True)
def reset_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OFCA_CREATOR_VAULT_INDEFINITE_GATE", raising=False)
    transport_manager.reset()
    with transport_manager.canonical_database.transaction() as connection:
        connection.execute("DELETE FROM managed_deletion_operations")
        connection.execute("DELETE FROM archive_membership")
        connection.execute("DELETE FROM participant_deletion_chat_scopes")
        connection.execute("DELETE FROM deletion_barriers")
        connection.execute("DELETE FROM archive_policies")
    app.dependency_overrides.clear()
    app.dependency_overrides[get_runtime_policy] = policy
    yield
    app.dependency_overrides.clear()
    transport_manager.reset()
    with transport_manager.canonical_database.transaction() as connection:
        connection.execute("DELETE FROM managed_deletion_operations")
        connection.execute("DELETE FROM archive_membership")
        connection.execute("DELETE FROM participant_deletion_chat_scopes")
        connection.execute("DELETE FROM deletion_barriers")
        connection.execute("DELETE FROM archive_policies")


def seed_message() -> None:
    now = NOW.isoformat()
    with transport_manager.canonical_database.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO account_heads(creator_account_id,updated_at) VALUES (?,?)",
            (DEV_ACCOUNT_ID, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO account_chats(
                   creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at,lifecycle_origin,lifecycle_started_at)
               VALUES (?,'chat-1','full','participant-1','Participant',?,'chat-hash',
                       1,1,'chat-event',0,?,'ordinary',?)""",
            (DEV_ACCOUNT_ID, now, now, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at,
                   lifecycle_origin,lifecycle_started_at)
               VALUES (?,'message-1','chat-1','participant-1','private text',?,'inbound',
                       NULL,'message-hash',1,1,'message-event',0,?,'ordinary',?)""",
            (DEV_ACCOUNT_ID, now, now, now),
        )


def message_exists() -> bool:
    with transport_manager.canonical_database.read() as connection:
        return connection.execute(
            "SELECT 1 FROM account_messages WHERE creator_account_id=? AND message_id='message-1'",
            (DEV_ACCOUNT_ID,),
        ).fetchone() is not None


def test_delete_all_reports_incomplete_and_retries_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_message()
    csrf = csrf_token(policy())
    original_projection = transport_manager.project_committed_state

    async def fail_projection(account_id: str):
        del account_id
        raise RuntimeError("injected projection failure")

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/settings/creator-vault/commands",
            headers={"X-CSRF-Token": csrf},
            json={"action": "enable_finite", "finite_horizon_days": 365},
        ).status_code == 200
        monkeypatch.setattr(transport_manager, "project_committed_state", fail_projection)
        deletion = client.post(
            "/api/v1/settings/creator-vault/commands",
            headers={"X-CSRF-Token": csrf},
            json={"action": "delete_all"},
        )
        assert deletion.status_code == 200
        operation = deletion.json()["deletion_operation"]
        assert operation["status"] == "incomplete"
        assert deletion.json()["status"]["deletion_operation"] == operation
        assert message_exists() is False
        operation_id = operation["operation_id"]

    monkeypatch.setattr(transport_manager, "project_committed_state", original_projection)
    with TestClient(app) as client:
        retry = client.post(
            f"/api/v1/settings/creator-vault/deletions/{operation_id}/retry",
            headers={"X-CSRF-Token": csrf},
        )
        assert retry.status_code == 200
        assert retry.json()["status"] == "complete"
        assert client.get("/api/v1/settings/creator-vault").json()["deletion_operation"] is None
        seed_message()
        assert message_exists() is False
