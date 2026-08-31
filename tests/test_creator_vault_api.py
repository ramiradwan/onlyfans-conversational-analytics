from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.security import AuthContext, csrf_token, get_runtime_policy
from app.main import app
from app.security.runtime_policy import AuthorizationEpoch, RuntimePolicy
from app.transport.manager import DEV_ACCOUNT_ID, DEV_PRINCIPAL_ID, transport_manager


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def reset_test_state() -> None:
    """Clear shared test state without weakening durable restart semantics."""
    transport_manager.reset()
    with transport_manager.canonical_database.transaction() as connection:
        connection.execute("DELETE FROM archive_membership")
        connection.execute("DELETE FROM participant_deletion_chat_scopes")
        connection.execute("DELETE FROM deletion_barriers")
        connection.execute("DELETE FROM archive_policies")


@pytest.fixture(autouse=True)
def reset_manager(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OFCA_CREATOR_VAULT_INDEFINITE_GATE", raising=False)
    reset_test_state()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_runtime_policy] = lambda: policy()
    yield
    app.dependency_overrides.clear()
    reset_test_state()


def policy(role: str = "creator") -> RuntimePolicy:
    return RuntimePolicy(
        identity=AuthContext(
            DEV_PRINCIPAL_ID,
            DEV_ACCOUNT_ID,
            role,
            "dev-platform-creator",
            "development-session" if role == "creator" else f"{role}-session",
        ),
        authorization_epoch=AuthorizationEpoch(0),
    )


def creator_csrf() -> str:
    return csrf_token(policy())


def seed_message(
    *,
    message_id: str = "message-1",
    chat_id: str = "chat-1",
    participant_id: str = "participant-1",
) -> None:
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
               VALUES (?,?,'full',?,'Participant',?,'chat-hash',1,1,'chat-event',0,?,
                       'ordinary',?)""",
            (DEV_ACCOUNT_ID, chat_id, participant_id, now, now, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at,
                   lifecycle_origin,lifecycle_started_at)
               VALUES (?,?,?,?,?,?,'inbound',NULL,'message-hash',1,1,'message-event',0,?,
                       'ordinary',?)""",
            (
                DEV_ACCOUNT_ID,
                message_id,
                chat_id,
                participant_id,
                "private text",
                now,
                now,
                now,
            ),
        )


def message_exists(message_id: str) -> bool:
    with transport_manager.canonical_database.read() as connection:
        return (
            connection.execute(
                """SELECT 1 FROM account_messages
                   WHERE creator_account_id=? AND message_id=?""",
                (DEV_ACCOUNT_ID, message_id),
            ).fetchone()
            is not None
        )


def post_command(client: TestClient, payload: dict, *, csrf: str | None = None):
    headers = {}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return client.post(
        "/api/v1/settings/creator-vault/commands",
        headers=headers,
        json=payload,
    )


def test_creator_vault_commands_are_creator_authorized_and_gate_indefinite() -> None:
    with TestClient(app) as client:
        initial = client.get("/api/v1/settings/creator-vault")
        assert initial.status_code == 200
        assert initial.headers["cache-control"] == "no-store"
        assert initial.json()["policy"] == {
            "enabled": False,
            "policy_type": "disabled",
            "finite_horizon_days": None,
            "revision": 0,
        }
        assert initial.json()["capabilities"]["indefinite_retention"] is False

        missing_csrf = post_command(
            client,
            {"action": "enable_finite", "finite_horizon_days": 365},
        )
        assert missing_csrf.status_code == 403

        enabled = post_command(
            client,
            {"action": "enable_finite", "finite_horizon_days": 365},
            csrf=creator_csrf(),
        )
        assert enabled.status_code == 200
        assert enabled.json()["status"]["policy"] == {
            "enabled": True,
            "policy_type": "finite",
            "finite_horizon_days": 365,
            "revision": 1,
        }

        indefinite = post_command(
            client,
            {"action": "enable_indefinite"},
            csrf=creator_csrf(),
        )
        assert indefinite.status_code == 409
        assert indefinite.json()["detail"] == "indefinite_retention_unavailable"

        disabled = post_command(
            client,
            {"action": "disable"},
            csrf=creator_csrf(),
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"]["policy"]["enabled"] is False
        assert disabled.json()["status"]["policy"]["policy_type"] == "disabled"

    app.dependency_overrides[get_runtime_policy] = lambda: policy("operator")
    with TestClient(app) as client:
        assert client.get("/api/v1/settings/creator-vault").status_code == 403
        denied = post_command(
            client,
            {"action": "enable_finite", "finite_horizon_days": 30},
            csrf=csrf_token(policy("operator")),
        )
        assert denied.status_code == 403


def test_indefinite_control_appears_only_when_production_gate_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFCA_CREATOR_VAULT_INDEFINITE_GATE", "open")
    with TestClient(app) as client:
        status = client.get("/api/v1/settings/creator-vault")
        assert status.status_code == 200
        assert status.json()["capabilities"]["indefinite_retention"] is True
        enabled = post_command(
            client,
            {"action": "enable_indefinite"},
            csrf=creator_csrf(),
        )
        assert enabled.status_code == 200
        assert enabled.json()["status"]["policy"]["policy_type"] == "indefinite_until_delete"


@pytest.mark.parametrize(
    ("action", "target_id", "seed_kwargs"),
    [
        ("delete_message", "message-1", {}),
        ("delete_conversation", "chat-1", {}),
        ("delete_participant", "participant-1", {}),
    ],
)
def test_selective_delete_commands_call_canonical_retention_authority(
    action: str,
    target_id: str,
    seed_kwargs: dict,
) -> None:
    seed_message(**seed_kwargs)
    with TestClient(app) as client:
        enabled = post_command(
            client,
            {"action": "enable_finite", "finite_horizon_days": 365},
            csrf=creator_csrf(),
        )
        assert enabled.status_code == 200
        deleted = post_command(
            client,
            {"action": action, "target_id": target_id},
            csrf=creator_csrf(),
        )
        assert deleted.status_code == 200
        assert deleted.json()["deletion_revision"] == 1
        assert message_exists("message-1") is False


def test_delete_all_and_unlink_require_explicit_archive_treatment() -> None:
    seed_message()
    with TestClient(app) as client:
        assert post_command(
            client,
            {"action": "enable_finite", "finite_horizon_days": 365},
            csrf=creator_csrf(),
        ).status_code == 200

        missing_treatment = post_command(
            client,
            {"action": "unlink"},
            csrf=creator_csrf(),
        )
        assert missing_treatment.status_code == 422

        preserved = post_command(
            client,
            {"action": "unlink", "unlink_archive_treatment": "preserve"},
            csrf=creator_csrf(),
        )
        assert preserved.status_code == 200
        assert preserved.json()["unlink_archive_treatment"] == "preserve"
        assert message_exists("message-1") is True

        deleted = post_command(
            client,
            {"action": "unlink", "unlink_archive_treatment": "delete"},
            csrf=creator_csrf(),
        )
        assert deleted.status_code == 200
        assert deleted.json()["unlink_archive_treatment"] == "delete"
        assert deleted.json()["deletion_revision"] == 1
        assert message_exists("message-1") is False


def test_delete_all_command_deletes_managed_vault_scope() -> None:
    seed_message()
    with TestClient(app) as client:
        assert post_command(
            client,
            {"action": "enable_finite", "finite_horizon_days": 365},
            csrf=creator_csrf(),
        ).status_code == 200
        all_deleted = post_command(
            client,
            {"action": "delete_all"},
            csrf=creator_csrf(),
        )
        assert all_deleted.status_code == 200
        assert all_deleted.json()["deletion_revision"] == 1
        assert message_exists("message-1") is False


def test_creator_vault_export_route_returns_truthful_attachment() -> None:
    seed_message()
    with TestClient(app) as client:
        assert post_command(
            client,
            {"action": "enable_finite", "finite_horizon_days": 365},
            csrf=creator_csrf(),
        ).status_code == 200

        response = client.get("/api/v1/settings/creator-vault/export")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"] == (
            'attachment; filename="creator-vault-export.json"'
        )
        document = response.json()
        assert document["manifest"]["export_type"] == "creator_vault"
        assert document["manifest"]["content"]["message_count"] == 1
        assert document["messages"][0]["text"] == "private text"
        assert document["manifest"]["copy_domains"]["managed_recovery"] == {
            "managed_by_product": True,
            "inspection_complete": False,
            "recognized_cohort_count": 0,
            "within_retention_cohort_count": 0,
            "expired_cohort_count": 0,
            "unrecognized_file_count": 0,
            "copies_observed": False,
            "copies_may_remain": True,
        }
