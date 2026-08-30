from __future__ import annotations

import base64
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.security.extension_storage import (
    extension_storage_key,
    open_extension_storage_bootstrap,
    seal_extension_storage_bootstrap,
)
from app.security.local_data_key import LocalDataKeyError
from app.transport.manager import DEV_ACCOUNT_ID, DEV_PRINCIPAL_ID, transport_manager


EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
OTHER_EXTENSION_ID = "bcdefghijklmnopabcdefghijklmnopa"
INSTALLATION_ID = UUID("20000000-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
def reset_transport() -> None:
    transport_manager.reset()
    yield
    transport_manager.reset()


def bootstrap(account_id: str = DEV_ACCOUNT_ID, ticket: str = "pair-secret") -> str:
    return seal_extension_storage_bootstrap(
        extension_id=EXTENSION_ID,
        creator_account_id=account_id,
        credential_kind="pairing",
        auth_ticket=ticket,
    )


def extension_headers(**extra: str) -> dict[str, str]:
    return {"Origin": f"chrome-extension://{EXTENSION_ID}", **extra}


def test_bootstrap_is_opaque_device_bound_and_account_keys_are_separated() -> None:
    sealed = bootstrap()
    assert DEV_ACCOUNT_ID not in sealed
    assert "pair-secret" not in sealed
    assert open_extension_storage_bootstrap(
        sealed,
        expected_extension_id=EXTENSION_ID,
    ).auth_ticket == "pair-secret"
    with pytest.raises(LocalDataKeyError):
        open_extension_storage_bootstrap(
            sealed,
            expected_extension_id=OTHER_EXTENSION_ID,
        )
    tampered = sealed[:-1] + ("A" if sealed[-1] != "A" else "B")
    with pytest.raises(LocalDataKeyError):
        open_extension_storage_bootstrap(
            tampered,
            expected_extension_id=EXTENSION_ID,
        )

    first = extension_storage_key(
        settings.auth_database_path,
        extension_id=EXTENSION_ID,
        creator_account_id=DEV_ACCOUNT_ID,
    )
    repeated = extension_storage_key(
        settings.auth_database_path,
        extension_id=EXTENSION_ID,
        creator_account_id=DEV_ACCOUNT_ID,
    )
    other = extension_storage_key(
        settings.auth_database_path,
        extension_id=EXTENSION_ID,
        creator_account_id="other-account",
    )
    assert first == repeated
    assert first != other
    assert len(first) == 32


def test_unseal_and_rotation_require_exact_extension_origin_and_matching_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "extension_id", EXTENSION_ID)
    initial_ticket = "pair-secret"
    sealed = bootstrap(ticket=initial_ticket)

    with TestClient(app, base_url="http://bridge.localhost:17871") as client:
        denied = client.post(
            "/api/v1/agent/storage/unseal",
            headers={"Origin": "chrome-extension://bcdefghijklmnopabcdefghijklmnopa"},
            json={"storage_bootstrap": sealed},
        )
        assert denied.status_code == 403

        unlocked = client.post(
            "/api/v1/agent/storage/unseal",
            headers=extension_headers(),
            json={"storage_bootstrap": sealed},
        )
        assert unlocked.status_code == 200
        assert unlocked.headers["cache-control"] == "no-store"
        document = unlocked.json()
        assert document == {
            "schema": "ofca-extension-storage-unlock/v1",
            "creator_account_id": DEV_ACCOUNT_ID,
            "credential_kind": "pairing",
            "auth_ticket": initial_ticket,
            "storage_key_base64": document["storage_key_base64"],
        }
        assert len(base64.b64decode(document["storage_key_base64"], validate=True)) == 32

        pairing, _ = transport_manager.issue_agent_pairing_ticket(
            principal_id=DEV_PRINCIPAL_ID,
            creator_account_id=DEV_ACCOUNT_ID,
        )
        _, _, reconnect, config = transport_manager.authenticate_agent_handshake(
            pairing,
            DEV_ACCOUNT_ID,
            INSTALLATION_ID,
        )
        rotated = client.post(
            "/api/v1/agent/storage/rotate",
            headers=extension_headers(Authorization=f"Bearer {config}"),
            json={
                "protocol_version": "2",
                "creator_account_id": DEV_ACCOUNT_ID,
                "agent_installation_id": str(INSTALLATION_ID),
                "reconnect_auth_ticket": reconnect,
                "storage_bootstrap": sealed,
            },
        )
        assert rotated.status_code == 200
        assert rotated.headers["cache-control"] == "no-store"
        rotated_bootstrap = rotated.json()["storage_bootstrap"]
        reopened = client.post(
            "/api/v1/agent/storage/unseal",
            headers=extension_headers(),
            json={"storage_bootstrap": rotated_bootstrap},
        )
        assert reopened.status_code == 200
        assert reopened.json()["credential_kind"] == "reconnect"
        assert reopened.json()["auth_ticket"] == reconnect

        wrong_config = client.post(
            "/api/v1/agent/storage/rotate",
            headers=extension_headers(Authorization="Bearer wrong"),
            json={
                "protocol_version": "2",
                "creator_account_id": DEV_ACCOUNT_ID,
                "agent_installation_id": str(INSTALLATION_ID),
                "reconnect_auth_ticket": reconnect,
                "storage_bootstrap": rotated_bootstrap,
            },
        )
        assert wrong_config.status_code == 401
