"""OFF-04. Prove the saved-data authority cannot mint a full-runtime session."""
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import saved_data, text_analysis_preview, webauthn
from app.api.security import get_runtime_policy
from app.persistence.auth import RevocationKey, RevocationScopeType, SQLiteAuthenticationStore
from app.persistence.retention import CreatorVaultRetention
from app.security.saved_data import SavedDataSessions
from app.security.webauthn import WebAuthnAuthorityPort, WebAuthnService
from test_creator_vault_export import database, seed_message
import test_creator_vault_export as vault_fixture
from test_webauthn_routes import (
    ACCOUNT_ID, CREDENTIAL_ID, INSTANT, ORIGIN, RP_ID, _authentication_payload,
    _register_public_key, _seeded_store,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    location = tmp_path / "auth.sqlite3"
    _seeded_store(location, INSTANT)
    clock = [INSTANT]
    store = SQLiteAuthenticationStore(location, clock=lambda: clock[0])
    key = ec.generate_private_key(ec.SECP256R1())
    _register_public_key(store, key)
    service = WebAuthnService(store, relying_party_id=RP_ID, expected_origin=ORIGIN, relying_party_name="Dipsy",
        challenge_ttl_seconds=60, session_ttl_seconds=120, clock=lambda: clock[0])
    port = WebAuthnAuthorityPort(store, clock=lambda: clock[0])
    app = FastAPI()
    app.include_router(saved_data.router)
    app.include_router(webauthn.router)
    app.include_router(text_analysis_preview.router)
    app.dependency_overrides[saved_data.require_local_installation] = lambda: None
    app.dependency_overrides[webauthn.require_activated_runtime] = lambda: None
    app.dependency_overrides[webauthn.authentication_store] = lambda: store
    app.dependency_overrides[webauthn.webauthn_service] = lambda: service
    app.dependency_overrides[webauthn.webauthn_authority_port] = lambda: port
    app.dependency_overrides[get_runtime_policy] = lambda: store.build_runtime_policy()
    monkeypatch.setattr(saved_data, "_sessions", SavedDataSessions())
    db = database(tmp_path / "canonical.sqlite3")
    monkeypatch.setattr(saved_data.transport_manager, "canonical_database", db)
    monkeypatch.setattr(vault_fixture, "ACCOUNT", ACCOUNT_ID)
    seed_message(db, text="OFF-04 synthetic archive item")
    seed_message(db, message_id="message-2", text="OFF-04 neighboring item")
    monkeypatch.setattr(saved_data.creator_vault, "_retention",
                        lambda: CreatorVaultRetention(db, clock=lambda: clock[0]))
    CreatorVaultRetention(db, clock=lambda: vault_fixture.NOW).set_policy(
        ACCOUNT_ID, "finite", finite_horizon_days=365, creator_action_ref="fixture-owner")
    with TestClient(app, base_url=ORIGIN.replace("http:", "https:"), headers={"Origin": ORIGIN}) as client:
        assert client.post(saved_data.PREFIX + "/available").json() == {"available": False}
        challenge = client.post("/api/v1/webauthn/login/begin").json()["challenge"]
        result = client.post("/api/v1/webauthn/login/finish", json=_authentication_payload(key, challenge))
        assert result.status_code == 200
        client.cookies.clear()
        yield client, store, key, clock


def test_saved_owner_can_view_export_delete_beyond_grace(workspace, monkeypatch):
    client, store, key, clock = workspace
    clock[0] += timedelta(days=30)
    api = saved_data.PREFIX
    assert client.post("/api/v1/webauthn/login/begin").status_code == 403
    assert client.post(api + "/available").json() == {"available": True}
    assert client.post(api + "/view").status_code == 403
    options = client.post(api + "/login/begin").json()
    assert options["userVerification"] == "required"
    assert options["allowCredentials"] == [{"type": "public-key", "id": CREDENTIAL_ID}]
    assertion = _authentication_payload(key, options["challenge"], counter=2)
    signed = client.post(api + "/login/finish", json=assertion)
    assert signed.status_code == 200
    csrf = {"x-csrf-token": signed.json()["csrf_token"]}
    assert client.post(api + "/login/finish", json=assertion).status_code == 403
    assert client.post(api + "/delete").status_code == 403
    assert store.read_bridge_session(client.cookies.get(saved_data.COOKIE)) is None
    assert webauthn.settings.bridge_session_cookie_name not in client.cookies
    assert client.post("/api/v1/webauthn/login/begin", headers=csrf).status_code == 403
    prepare = {"external_key": "51b95f39-20aa-4373-ad82-52d00c377818", "expected_profile_sha256": "0" * 64}
    assert client.post("/api/v1/nlp/previews", headers=csrf, json=prepare).status_code == 401
    assert client.post(api + "/view", headers=csrf).json()["messages"][0]["text"] == "OFF-04 synthetic archive item"
    export = client.post(api + "/export", headers=csrf)
    assert "attachment" in export.headers["content-disposition"]
    assert export.json()["messages"][0]["text"] == "OFF-04 synthetic archive item"
    assert client.post(api + "/delete", headers=csrf).status_code == 200
    assert client.post(api + "/export", headers=csrf).json()["messages"] == []
    monkeypatch.setattr(saved_data, "_sessions", SavedDataSessions())
    assert client.post(api + "/view", headers=csrf).status_code == 403
    challenge = client.post(api + "/login/begin").json()["challenge"]
    signed = client.post(api + "/login/finish", json=_authentication_payload(key, challenge, counter=3))
    csrf = {"x-csrf-token": signed.json()["csrf_token"]}
    assert client.post(api + "/view", headers=csrf).json()["messages"] == []
    store.revoke(RevocationKey(RevocationScopeType.WEBAUTHN_CREDENTIAL, CREDENTIAL_ID), reason="owner removed passkey")
    assert client.post(api + "/export", headers=csrf).status_code == 403
    assert client.post(api + "/login/begin").status_code == 403


def test_challenges_are_bound_to_their_purpose_and_original_key(workspace):
    client, _, key, _ = workspace
    api = saved_data.PREFIX
    normal = client.post("/api/v1/webauthn/login/begin").json()["challenge"]
    saved = client.post(api + "/login/begin").json()["challenge"]
    assert client.post(api + "/login/finish", json=_authentication_payload(key, normal, counter=2)).status_code == 403
    wrong = _authentication_payload(ec.generate_private_key(ec.SECP256R1()), saved, counter=2)
    assert client.post(api + "/login/finish", json=wrong).status_code == 403
    assert client.post(api + "/view").status_code == 403
    result = client.post("/api/v1/webauthn/login/finish", json=_authentication_payload(key, saved, counter=2))
    assert result.status_code == 400
    assert not client.cookies


def test_one_message_deletion_preserves_neighbor_and_checks_origin(workspace, monkeypatch):
    client, _, key, clock = workspace
    clock[0] += timedelta(days=30)
    from app.api import security
    monkeypatch.setattr(security, "_development_context_allowed", lambda: False)
    monkeypatch.setattr(security.settings, "bridge_origin", ORIGIN)
    api = saved_data.PREFIX
    # Abandoning the Windows ceremony leaves every data operation locked.
    assert client.post(api + "/login/begin").status_code == 200
    assert client.post(api + "/export").status_code == 403
    options = client.post(api + "/login/begin").json()
    response = client.post(api + "/login/finish", json=_authentication_payload(key, options["challenge"], counter=2))
    assert response.status_code == 200
    csrf = {"x-csrf-token": response.json()["csrf_token"]}
    target = api + "/messages/delete?message_id=message-1"
    assert client.post(target, headers={**csrf, "Origin": "https://different.example"}).status_code == 403
    assert client.post(target, headers=csrf).json() == {"deleted": True}
    assert client.post(target, headers=csrf).status_code == 404
    assert [m["text"] for m in client.post(api + "/export", headers=csrf).json()["messages"]] == [
        "OFF-04 neighboring item"]
    assert client.post(api + "/lock", headers=csrf).json() == {"locked": True}
    assert client.post(api + "/view", headers=csrf).status_code == 403


def test_reading_never_extends_saved_session(workspace, monkeypatch):
    client, _, key, _ = workspace
    now = [0]
    monkeypatch.setattr(saved_data, "_sessions", SavedDataSessions(clock=lambda: now[0]))
    api = saved_data.PREFIX
    challenge = client.post(api + "/login/begin").json()["challenge"]
    issued = client.post(api + "/login/finish", json=_authentication_payload(key, challenge, counter=2)).json()
    csrf = {"x-csrf-token": issued["csrf_token"]}
    now[0] = issued["expires_in"] - 1
    assert client.post(api + "/view", headers=csrf).status_code == 200
    now[0] += 1
    assert client.post(api + "/view", headers=csrf).status_code == 403


@pytest.mark.parametrize("change", ["new_key", "withdrawn_owner", "operator", "account_removed"])
def test_changed_local_owner_is_denied(workspace, change):
    client, store, _, clock = workspace
    clock[0] += timedelta(days=30)
    with store.database.transaction() as connection:
        if change == "new_key":
            connection.execute("UPDATE installation_key_reference SET installation_key_jkt='new'")
        elif change == "withdrawn_owner":
            connection.execute("UPDATE verified_grant_references SET revoked_at=expires_at WHERE grant_type='membership_snapshot'")
        elif change == "operator":
            connection.execute("""UPDATE verified_grant_references SET membership_roles='["creator_operator"]'
                                  WHERE grant_type='membership_snapshot'""")
        else:
            connection.execute("""UPDATE verified_grant_references SET allowed_creator_account_ids='[]'
                                  WHERE grant_type='membership_snapshot'""")
    assert client.post(saved_data.PREFIX + "/available").json() == {"available": False}
    assert client.post(saved_data.PREFIX + "/login/begin").status_code == 403
