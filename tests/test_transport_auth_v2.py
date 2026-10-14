from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import Settings, settings
from app.onboarding.admission import AdmissionRefused, ScopedAdmissions
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.factory import create_canonical_repositories
from app.services.agent_configuration import AgentConfigurationAuthority, InMemoryAgentConfigRepository
from app.transport.manager import AuthenticationError, InMemoryTransportManager

pytestmark = [pytest.mark.ci_tier('integration')]


NOW = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
ACCOUNT = "creator-account-1"
PRINCIPAL = "principal-1"


def manager() -> InMemoryTransportManager:
    return InMemoryTransportManager(create_canonical_repositories("memory"))


def test_pairing_is_single_use_and_reconnect_survives_manager_recreation() -> None:
    first = manager()
    installation = uuid4()
    pairing, _ = first.issue_agent_pairing_ticket(
        principal_id=PRINCIPAL,
        creator_account_id=ACCOUNT,
        now=NOW,
    )
    principal, account, reconnect, config = first.authenticate_agent_handshake(
        pairing, ACCOUNT, installation, now=NOW
    )
    assert (principal, account) == (PRINCIPAL, ACCOUNT)
    assert reconnect != config
    with pytest.raises(AuthenticationError, match="already used"):
        first.authenticate_agent_handshake(pairing, ACCOUNT, installation, now=NOW)

    recreated = manager()
    renewed = recreated.authenticate_agent_handshake(
        reconnect, ACCOUNT, installation, now=NOW + timedelta(minutes=1)
    )
    assert renewed[:2] == (PRINCIPAL, ACCOUNT)
    assert renewed[2] != reconnect
    assert recreated.authenticate_agent_config(
        config, ACCOUNT, installation, now=NOW + timedelta(minutes=1)
    ) == (PRINCIPAL, ACCOUNT)


def test_agent_tickets_reject_expiry_purpose_account_and_installation_mismatch() -> None:
    first = manager()
    installation = uuid4()
    pairing, _ = first.issue_agent_pairing_ticket(
        principal_id=PRINCIPAL, creator_account_id=ACCOUNT, now=NOW
    )
    _, _, reconnect, config = first.authenticate_agent_handshake(
        pairing, ACCOUNT, installation, now=NOW
    )
    with pytest.raises(AuthenticationError):
        first.authenticate_agent_handshake(config, ACCOUNT, installation, now=NOW)
    with pytest.raises(AuthenticationError):
        first.authenticate_agent_config(reconnect, ACCOUNT, installation, now=NOW)
    with pytest.raises(AuthenticationError):
        first.authenticate_agent_handshake(reconnect, "other-account", installation, now=NOW)
    with pytest.raises(AuthenticationError):
        first.authenticate_agent_handshake(reconnect, ACCOUNT, uuid4(), now=NOW)
    with pytest.raises(AuthenticationError, match="expired"):
        first.authenticate_agent_handshake(
            reconnect,
            ACCOUNT,
            installation,
            now=NOW + timedelta(seconds=settings.agent_reconnect_ticket_ttl_seconds + 1),
        )


def test_bridge_page_ticket_remains_valid_past_two_minute_pairing_window() -> None:
    first = manager()
    ticket = first.issue_bridge_ticket(
        principal_id=PRINCIPAL,
        creator_account_id=ACCOUNT,
        role="operator",
        now=NOW,
    )
    assert first.authenticate(
        ticket,
        ACCOUNT,
        role="bridge",
        now=NOW + timedelta(minutes=10),
    ) == (PRINCIPAL, ACCOUNT)
    with pytest.raises(AuthenticationError, match="expired"):
        first.authenticate(
            ticket,
            ACCOUNT,
            role="bridge",
            now=NOW + timedelta(seconds=settings.bridge_ticket_ttl_seconds + 1),
        )


def test_candidate_defaults_require_explicit_exact_launcher_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CANONICAL_PERSISTENCE_BACKEND", raising=False)
    with pytest.raises(ValueError, match="launcher bootstrap token"):
        Settings(
            environment="production",
            security_signing_secret="production-signing-secret-value-1234",
            websocket_auth_mode="local_session",
        )
    configured = Settings(
        environment="production",
        security_signing_secret="production-signing-secret-value-1234",
        websocket_auth_mode="local_session",
        local_session_bootstrap_token="x" * 32,
        local_principal_id=PRINCIPAL,
        identity_binding_source="verified_grants",
        extension_id="abcdefghijklmnopabcdefghijklmnop",
    )
    assert configured.canonical_persistence_backend == "sqlite"
    with pytest.raises(ValueError, match="generated security signing secret"):
        Settings(
            environment="production",
            security_signing_secret="replace-with-at-least-32-random-characters",
            websocket_auth_mode="local_session",
            local_session_bootstrap_token="x" * 32,
            local_principal_id=PRINCIPAL,
            identity_binding_source="verified_grants",
            extension_id="abcdefghijklmnopabcdefghijklmnop",
        )
    with pytest.raises(ValueError, match="exact non-placeholder"):
        Settings(
            environment="production",
            security_signing_secret="production-signing-secret-value-1234",
            websocket_auth_mode="local_session",
            local_session_bootstrap_token="x" * 32,
            local_principal_id="replace-with-provisioned-principal",
            identity_binding_source="verified_grants",
            extension_id="abcdefghijklmnopabcdefghijklmnop",
        )
    with pytest.raises(ValueError, match="Chrome extension ID"):
        Settings(
            environment="production",
            security_signing_secret="production-signing-secret-value-1234",
            websocket_auth_mode="local_session",
            local_session_bootstrap_token="x" * 32,
            local_principal_id=PRINCIPAL,
            identity_binding_source="verified_grants",
        )
    # No account is addressable as installation configuration, so the bootstrap
    # account can only arrive through the injected seam.
    assert not {"local_creator_account_id", "local_platform_creator_id"} & set(
        Settings.model_fields
    )
    assert AgentConfigurationAuthority(
        InMemoryAgentConfigRepository()
    ).bootstrap_account_id != ACCOUNT
    authority = AgentConfigurationAuthority(
        InMemoryAgentConfigRepository(), bootstrap_account_id=ACCOUNT
    )
    assert authority.required_document(ACCOUNT).creator_account_id == ACCOUNT
    with pytest.raises(LookupError):
        authority.required_document("dev-creator-account")


def test_desktop_launcher_entry_consumption_survives_authority_recreation(
    tmp_path: Path,
) -> None:
    # Native launcher admission now belongs to the durable authentication owner.
    # Reopen the real store as well as its authority; a fresh process must not
    # replay an entry or inherit plaintext launcher/session credentials.
    path = tmp_path / "auth.sqlite3"
    first = ScopedAdmissions(SQLiteAuthenticationStore(path), stage="runtime")
    code = first.prepare_desktop()
    with first.authentication.database.read() as connection:
        entries = repr([tuple(row) for row in connection.execute("SELECT * FROM desktop_admission_entries")])
    assert code not in entries
    session = first.consume(code, role="desktop")
    recreated = ScopedAdmissions(SQLiteAuthenticationStore(path), stage="runtime")
    with pytest.raises(AdmissionRefused):
        recreated.consume(code, role="desktop")
    assert recreated.require(session.identifier, role="desktop") == session
    assert session.journey_id is None
    with recreated.authentication.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_admission_entries").fetchone()[0] == 0
        rows = [tuple(row) for row in connection.execute("SELECT * FROM desktop_admission_sessions")]
    assert len(rows) == 1
    assert code not in repr(rows) and session.identifier not in repr(rows)
