"""Durable completion of provisioning through the isolated surface."""

from __future__ import annotations

import hashlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from app import packaged_entry
from app.core.runtime_paths import runtime_configuration_file, runtime_data_directory
from app.persistence.auth import (
    AuthenticationStore,
    AuthorizedAccountBinding,
    InstallationKeyReference,
    InstallationKeyReservation,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.provisioning.app import PROVISIONING_FINALIZE_PATH, PROVISIONING_STATUS_PATH
from app.provisioning.completion import (
    durable_authentication_store,
    durable_completion_reader,
    durable_finalize_action,
)
from app.provisioning.finalize import FinalizationRequest, finalize_provisioning
from app.provisioning.session import (
    PROVISIONING_CSRF_HEADER,
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
)
from app.security.grant_types import PROVISIONING_GRANT_TYPES


ORGANIZATION_ID = "organization-1"
INSTALLATION_ID = "installation-1"
INSTALLATION_KEY_ID = "installation-key-1"
INSTALLATION_KEY_JKT = "installation-key-thumbprint-1"
ISSUER = "https://identity.example"
SUBJECT = "external-subject-1"
ACCOUNT_ID = "creator-account-1"
OTHER_ACCOUNT_ID = "creator-account-2"
ASSOCIATION_REQUEST_ID = "association-1"
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
HANDOFF_TOKEN = "t" * 32
# Stated independently of the module under test so that moving the store
# cannot silently move the case that pins it to installation configuration.
DATABASE_FILENAME = "auth.sqlite3"


@pytest.fixture
def data_directory(tmp_path: Path) -> Path:
    return runtime_data_directory(tmp_path / "runtime-data")


@pytest.fixture
def open_store(data_directory: Path):
    return durable_authentication_store(data_directory)


@pytest.fixture
def store(open_store) -> AuthenticationStore:
    return open_store()


def instant() -> datetime:
    return datetime.now(timezone.utc)


def grant(
    grant_type: str,
    *,
    creator_account_id: str = ACCOUNT_ID,
) -> VerifiedGrantReference:
    now = instant()
    reference_id = f"{grant_type}-current"
    membership = grant_type == "membership_snapshot"
    binding = grant_type == "creator_account_binding"
    entitlement = grant_type == "license_entitlement"
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"{reference_id}-identifier",
        grant_type=grant_type,
        grant_digest=hashlib.sha256(reference_id.encode()).hexdigest(),
        issuer=ISSUER,
        subject=SUBJECT,
        installation_id=INSTALLATION_ID,
        creator_account_id=creator_account_id if binding else None,
        valid_from=now - timedelta(hours=2),
        expires_at=now + timedelta(hours=2),
        verified_at=now - timedelta(minutes=5),
        organization_id=ORGANIZATION_ID,
        installation_key_id=INSTALLATION_KEY_ID,
        installation_key_jkt=INSTALLATION_KEY_JKT,
        membership_id="membership-1" if membership else None,
        approval_id="approval-1" if binding else None,
        approval_revision=1 if binding else None,
        entitlement_id="entitlement-1" if entitlement else None,
        product_id="product-1" if entitlement else None,
        allowed_creator_account_ids=(ACCOUNT_ID,) if membership else None,
        membership_roles=("owner",) if membership else None,
    )


def seed_verified_installation(
    store: AuthenticationStore, *, approved: bool = True
) -> tuple[str, ...]:
    """Record the installation key, the verified tuple, and one candidate."""

    now = instant()
    store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            created_at=now - timedelta(minutes=10),
        )
    )
    store.activate_installation_key(
        InstallationKeyReference(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            installation_key_id=INSTALLATION_KEY_ID,
            installation_key_jkt=INSTALLATION_KEY_JKT,
            public_key_jwk='{"kty":"EC"}',
            created_at=now - timedelta(minutes=10),
            activated_at=now - timedelta(minutes=9),
        )
    )
    references: list[str] = []
    for grant_type in PROVISIONING_GRANT_TYPES:
        reference = grant(grant_type)
        store.record_verified_grant(reference)
        references.append(reference.reference_id)
    record_candidate(store, approved=approved)
    return tuple(references)


def record_candidate(
    store: AuthenticationStore,
    *,
    association_request_id: str = ASSOCIATION_REQUEST_ID,
    creator_account_id: str = ACCOUNT_ID,
    approved: bool = True,
) -> None:
    now = instant()
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=association_request_id,
            installation_id=INSTALLATION_ID,
            onboarding_transaction_id=f"onboarding-{association_request_id}",
            organization_id=ORGANIZATION_ID,
            creator_account_id=creator_account_id,
            state=ProvisioningCandidateState.PENDING,
            requested_at=now - timedelta(minutes=3),
        )
    )
    if approved:
        store.approve_provisioning_candidate(
            association_request_id, resolved_at=now - timedelta(minutes=1)
        )


def finalize_action(open_store, data_directory: Path, *, extension_id: str = EXTENSION_ID):
    return durable_finalize_action(
        open_store, extension_id=extension_id, data_directory=data_directory
    )


def finalize(open_store, data_directory: Path, **overrides) -> str | None:
    return finalize_action(open_store, data_directory, **overrides)(
        association_request_id=ASSOCIATION_REQUEST_ID,
        detected_creator_account_id=ACCOUNT_ID,
        reported_platform_creator_id=None,
    )


def authorize_other_account(store: AuthenticationStore) -> None:
    """Occupy the single authorization slot with another account."""

    record_candidate(
        store,
        association_request_id="association-2",
        creator_account_id=OTHER_ACCOUNT_ID,
    )
    store.record_authorized_account_binding(
        AuthorizedAccountBinding(
            creator_account_id=OTHER_ACCOUNT_ID,
            installation_id=INSTALLATION_ID,
            platform_creator_id=OTHER_ACCOUNT_ID,
            association_request_id="association-2",
            grant_bundle_sha256="a" * 64,
            authorized_at=instant() - timedelta(minutes=2),
            grant_reference_ids=("membership_snapshot-current",),
        )
    )


def test_the_provisioning_store_is_opened_once_where_configuration_pins_it(
    open_store, data_directory: Path
) -> None:
    store = open_store()

    assert open_store() is store
    assert Path(store.database.path) == data_directory / DATABASE_FILENAME


def test_finalization_writes_configuration_and_authorizes_the_account(
    open_store, store: AuthenticationStore, data_directory: Path
) -> None:
    references = seed_verified_installation(store)

    assert finalize(open_store, data_directory) is None

    configuration = runtime_configuration_file(data_directory)
    assert configuration.exists()
    bindings = store.authorized_account_bindings()
    assert [binding.creator_account_id for binding in bindings] == [ACCOUNT_ID]
    assert bindings[0].association_request_id == ASSOCIATION_REQUEST_ID
    assert sorted(bindings[0].grant_reference_ids) == sorted(references)
    # Provisioning wrote the store configuration then pins as the runtime one.
    values = dotenv_values(configuration, interpolate=False)
    assert values["AUTH_DATABASE_PATH"] == str(store.database.path)
    assert values["LOCAL_PRINCIPAL_ID"] == SUBJECT


def test_stopping_between_the_two_writes_leaves_a_bootable_installation(
    open_store, store: AuthenticationStore, data_directory: Path, monkeypatch
) -> None:
    """Configuration is written first, so an interrupted run authorizes nothing.

    The first write alone is the state a crash leaves. Activation reads
    installation identity and never an authorized account, so what remains
    boots; it acts for no account until finalization is completed.
    """

    completed = durable_completion_reader(open_store, data_directory=data_directory)
    seed_verified_installation(store)

    finalize_provisioning(
        store=store,
        request=FinalizationRequest(
            association_request_id=ASSOCIATION_REQUEST_ID,
            detected_creator_account_id=ACCOUNT_ID,
        ),
        extension_id=EXTENSION_ID,
        data_directory=data_directory,
    )

    assert runtime_configuration_file(data_directory).exists()
    assert store.authorized_account_bindings() == ()
    assert completed() is False
    sentinel = object()
    main_module = types.ModuleType("app.main")
    main_module.app = sentinel
    monkeypatch.setitem(sys.modules, "app.main", main_module)
    assert packaged_entry.select_brain_application(data_directory) is sentinel


def test_a_refused_account_authorization_keeps_the_configuration_it_wrote(
    open_store, store: AuthenticationStore, data_directory: Path
) -> None:
    seed_verified_installation(store)
    authorize_other_account(store)

    refusal = finalize(open_store, data_directory)

    assert refusal == "additional_account_binding_unsupported"
    assert runtime_configuration_file(data_directory).exists()
    assert [
        binding.creator_account_id for binding in store.authorized_account_bindings()
    ] == [OTHER_ACCOUNT_ID]


def test_an_account_record_without_configuration_is_not_completion(
    open_store, store: AuthenticationStore, data_directory: Path
) -> None:
    completed = durable_completion_reader(open_store, data_directory=data_directory)
    seed_verified_installation(store)
    authorize_other_account(store)

    assert not runtime_configuration_file(data_directory).exists()
    assert completed() is False


def test_completion_is_reported_once_both_writes_land(
    open_store, store: AuthenticationStore, data_directory: Path
) -> None:
    completed = durable_completion_reader(open_store, data_directory=data_directory)
    seed_verified_installation(store)

    assert completed() is False
    assert finalize(open_store, data_directory) is None
    assert completed() is True


def test_a_revoked_authorization_still_reports_completed_finalization(
    open_store, store: AuthenticationStore, data_directory: Path
) -> None:
    """The store refuses to record a second account, so completion is permanent."""

    completed = durable_completion_reader(open_store, data_directory=data_directory)
    seed_verified_installation(store)
    assert finalize(open_store, data_directory) is None

    assert store.revoke_authorized_account_binding(ACCOUNT_ID) is True

    assert completed() is True


def test_an_absent_extension_identity_refuses_before_any_write(
    open_store, store: AuthenticationStore, data_directory: Path
) -> None:
    seed_verified_installation(store)

    refusal = finalize(open_store, data_directory, extension_id="")

    assert refusal == "installation_identity_unavailable"
    assert not runtime_configuration_file(data_directory).exists()
    assert store.authorized_account_bindings() == ()


def test_existing_configuration_from_other_bindings_refuses_as_a_conflict(
    open_store, store: AuthenticationStore, data_directory: Path
) -> None:
    seed_verified_installation(store)
    configuration = runtime_configuration_file(data_directory)
    configuration.write_text('LOCAL_PRINCIPAL_ID="other-subject"\n', encoding="utf-8")

    refusal = finalize(open_store, data_directory)

    assert refusal == "configuration_conflict"
    assert store.authorized_account_bindings() == ()


def test_an_unapproved_association_refuses_without_writing(
    open_store, store: AuthenticationStore, data_directory: Path
) -> None:
    seed_verified_installation(store, approved=False)

    refusal = finalize(open_store, data_directory)

    assert refusal == "account_approval_missing"
    assert not runtime_configuration_file(data_directory).exists()
    assert store.authorized_account_bindings() == ()


def test_the_composed_provisioning_surface_finalizes_and_then_reports_restart(
    data_directory: Path, monkeypatch
) -> None:
    """The surface packaged boot builds reaches durable finalization itself."""

    monkeypatch.setenv(
        packaged_entry.PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE, HANDOFF_TOKEN
    )
    monkeypatch.setenv(
        packaged_entry.PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE, EXTENSION_ID
    )
    store = SQLiteAuthenticationStore(_prepared(data_directory) / DATABASE_FILENAME)
    seed_verified_installation(store)

    application = packaged_entry.select_brain_application(data_directory)
    client = TestClient(application, base_url=PROVISIONING_ORIGIN)
    handoff = client.post(
        "/api/v1/provisioning/handoff",
        headers={"Authorization": "Provisioning " + HANDOFF_TOKEN},
    )
    redeemed = client.get(
        f"/provisioning/handoff?code={handoff.json()['handoff_code']}",
        follow_redirects=False,
    )
    cookie = {
        "Cookie": (
            f"{PROVISIONING_SESSION_COOKIE_NAME}="
            f"{redeemed.cookies[PROVISIONING_SESSION_COOKIE_NAME]}"
        )
    }
    token = client.get("/provisioning", headers=cookie).text.split(
        'data-provisioning-csrf="'
    )[1].split('"')[0]

    assert client.get(PROVISIONING_STATUS_PATH, headers=cookie).json() == {
        "state": "provisioning_ready"
    }

    finalized = client.post(
        PROVISIONING_FINALIZE_PATH,
        json={
            "association_request_id": ASSOCIATION_REQUEST_ID,
            "detected_creator_account_id": ACCOUNT_ID,
            "reported_platform_creator_id": None,
        },
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert finalized.status_code == 200
    assert finalized.json() == {"state": "configured_restart"}
    assert runtime_configuration_file(data_directory).exists()
    assert [
        binding.creator_account_id for binding in store.authorized_account_bindings()
    ] == [ACCOUNT_ID]
    assert client.get(PROVISIONING_STATUS_PATH, headers=cookie).json() == {
        "state": "configured_restart"
    }


def _prepared(data_directory: Path) -> Path:
    data_directory.mkdir(parents=True, exist_ok=True)
    return data_directory
