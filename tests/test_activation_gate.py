"""Covers the production fail-closed conditions for local runtime activation."""

from __future__ import annotations

import hashlib
import json
import sqlite3 as plaintext_sqlite
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.persistence import sqlite_api as cipher_sqlite
from app.persistence.auth import (
    AuthorizedAccountBinding,
    InstallationKeyReference,
    InstallationKeyReservation,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.security.account_bindings import (
    AccountResolutionRefused,
    eligible_account_for_policy,
    eligible_accounts,
    sole_eligible_account,
)
from app.security.activation_gate import (
    AUTHENTICATION_STORE,
    DEVELOPMENT_AUTHENTICATION,
    DEVELOPMENT_PRINCIPAL_ID,
    LOOPBACK_EXPOSURE,
    NON_EXPORTABLE_INSTALLATION_KEY,
    PINNED_SIGNING_TRUST,
    PRODUCTION_TRUST_SET_PATH,
    REQUIRED_GRANTS,
    ActivationDecision,
    ActivationInputs,
    ConditionOutcome,
    RuntimeConfiguration,
    evaluate_activation,
    record_activation,
    reset_activation,
    runtime_configuration,
    runtime_is_activated,
)
from app.security.grant_verifier import (
    GrantVerificationContext,
    load_pinned_trust_set,
    verify_grant,
)
from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    PLATFORM_CRYPTO_PROVIDER,
    InstallationKeyPolicyError,
    InstallationKeyUnavailable,
    ProviderKeyInfo,
)
from app.security.runtime_policy import AuthContext
from app.transport.manager import (
    DEV_ACCOUNT_ID,
    DEV_BRIDGE_AUTH_TICKET,
    DEV_PRINCIPAL_ID,
    AuthenticationError,
    transport_manager,
)


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "grant-profile-v1"
FIXTURE_TRUST_SET_PATH = "grant-profile-v1/keys/trust-set.json"
PROVIDER_KEY_NAME = "bridge-clean.installation.v1"
# Stated independently of the gate's own set, so removing a type from the gate
# fails this file's agreement instead of deleting the case that covers it.
ACTIVATION_GRANT_TYPES = ("installation_grant", "membership_snapshot")
# What a fully provisioned installation holds. The account binding is among
# them; activation must be satisfied without it.
RECORDED_GRANT_TYPES = (
    "installation_grant",
    "membership_snapshot",
    "creator_account_binding",
)
ACCOUNT_ID = "creator-account-fixture-001"
INSTALLATION_KEY_ID = "ik1.jcWDzvJIkKO1XtDL-Mp1vw"
INSTALLATION_KEY_JKT = "jcWDzvJIkKO1XtDL-Mp1vyrlHsT8kuAyj4DYblC1ByA"
VECTOR_INSTANT = datetime.fromtimestamp(1_784_336_400, timezone.utc)

REQUIRED_TABLES = (
    "agent_pairings",
    "auth_challenges",
    "auth_revocation_bindings",
    "auth_revocation_state",
    "authorization_epoch",
    "bridge_sessions",
    "installation_key_reference",
    "runtime_tickets",
    "verified_grant_references",
)
UNIQUE_DIGEST_COLUMNS = {
    "auth_challenges": "secret_digest",
    "runtime_tickets": "secret_digest",
    "verified_grant_references": "grant_digest",
}


class GrantAdmissionRefused(RuntimeError):
    """Raised when a contract vector does not pass grant verification."""


# --------------------------------------------------------------------------
# Contract vectors
# --------------------------------------------------------------------------


def _vector(grant_type: str, case: str) -> dict[str, Any]:
    directory = CONTRACTS / grant_type / case
    return {
        "token": (directory / "token.jws").read_text(encoding="ascii").strip(),
        "payload": json.loads(
            (directory / "payload.json").read_text(encoding="utf-8")
        ),
        "context": json.loads(
            (directory / "verification-context.json").read_text(encoding="utf-8")
        ),
    }


def _verification_context(document: dict[str, Any]) -> GrantVerificationContext:
    return GrantVerificationContext(
        expected_grant_type=document["expected_grant_type"],
        expected_audience=document["expected_audience"],
        expected_organization_id=document["expected_organization_id"],
        expected_installation_id=document["expected_installation_id"],
        expected_installation_key_id=document["expected_installation_key_id"],
        expected_installation_key_jkt=document["expected_installation_key_jkt"],
        expected_subject=document["expected_subject"],
        verifier_time=document["fixed_verifier_time"],
        tombstones=document["tombstones"],
        requested_operation=document["requested_operation"],
        requested_creator_account_ids=document["requested_creator_account_ids"],
    )


def _admit(
    grant_type: str, case: str, *, verified_at: datetime
) -> VerifiedGrantReference:
    """Verify one contract vector and build the reference an admission records.

    Verification precedes the reference in the same order the hosted grant
    client uses: a refused token yields no reference at all.
    """

    vector = _vector(grant_type, case)
    result = verify_grant(
        vector["token"],
        context=_verification_context(vector["context"]),
        trust_set=load_pinned_trust_set(
            FIXTURE_TRUST_SET_PATH, environment="development"
        ),
    )
    if not result.valid:
        raise GrantAdmissionRefused(result.result)
    payload = vector["payload"]
    membership = _vector("membership_snapshot", "valid-current")["payload"]
    return VerifiedGrantReference(
        reference_id=f"vg1.{grant_type}",
        grant_identifier=payload["jti"],
        grant_type=grant_type,
        grant_digest=hashlib.sha256(vector["token"].encode("ascii")).hexdigest(),
        issuer=membership["ciam_issuer"],
        subject=membership["ciam_subject"],
        installation_id=payload["installation_id"],
        creator_account_id=(
            payload["creator_account_id"]
            if grant_type == "creator_account_binding"
            else None
        ),
        valid_from=datetime.fromtimestamp(payload["nbf"], timezone.utc),
        expires_at=datetime.fromtimestamp(payload["exp"], timezone.utc),
        verified_at=verified_at,
        organization_id=payload["organization_id"],
        installation_key_id=payload["installation_key_id"],
        installation_key_jkt=payload["installation_key_jkt"],
        membership_id=(
            payload["membership_id"] if grant_type == "membership_snapshot" else None
        ),
        approval_id=(
            payload["approval_id"]
            if grant_type == "creator_account_binding"
            else None
        ),
        approval_revision=(
            payload["approval_revision"]
            if grant_type == "creator_account_binding"
            else None
        ),
        allowed_creator_account_ids=(
            tuple(payload["allowed_creator_account_ids"])
            if grant_type == "membership_snapshot"
            else None
        ),
    )


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class EmulatedKeyProvider:
    """Installation key provider double reporting one persisted key."""

    def __init__(
        self,
        public_key: ec.EllipticCurvePublicKey,
        *,
        provider_name: str = PLATFORM_CRYPTO_PROVIDER,
        export_policy: int = 0,
        hardware_backed: bool = True,
        present: bool = True,
    ) -> None:
        self.provider_name = provider_name
        self.export_policy = export_policy
        self.hardware_backed = hardware_backed
        self.present = present
        numbers = public_key.public_numbers()
        self._x = numbers.x.to_bytes(32, "big")
        self._y = numbers.y.to_bytes(32, "big")

    def key_info(self, provider_key_name: str) -> ProviderKeyInfo | None:
        if not self.present:
            return None
        return ProviderKeyInfo(
            provider_name=self.provider_name,
            provider_key_name=provider_key_name,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            export_policy=self.export_policy,
            hardware_backed=self.hardware_backed,
            x=self._x,
            y=self._y,
        )

    def create_non_exportable_key(self, provider_key_name: str) -> ProviderKeyInfo:
        raise NotImplementedError

    def sign_digest(self, provider_key_name: str, digest: bytes) -> bytes:
        raise NotImplementedError


class UnreadableStore:
    """Authentication store double whose grant read cannot be answered."""

    def __init__(self, reference: InstallationKeyReference) -> None:
        self._reference = reference

    def installation_key_reference(self) -> InstallationKeyReference:
        return self._reference

    def verified_grants(self) -> tuple[VerifiedGrantReference, ...]:
        raise cipher_sqlite.OperationalError("database is locked")


class PlainDatabase:
    """Database handle whose connection pragmas the test controls."""

    def __init__(self, path: Path, pragmas: dict[str, str]) -> None:
        self.path = path
        self._pragmas = pragmas

    @contextmanager
    def read(self) -> Iterator[plaintext_sqlite.Connection]:
        connection = plaintext_sqlite.connect(self.path)
        connection.row_factory = plaintext_sqlite.Row
        try:
            for name, value in self._pragmas.items():
                connection.execute(f"PRAGMA {name} = {value}")
            yield connection
        finally:
            connection.close()

    def validate_integrity(self) -> None:
        return None


class PlainStore:
    """Authentication store stand-in over a database the test shapes."""

    def __init__(self, database: PlainDatabase) -> None:
        self.database = database

    def installation_key_reference(self) -> InstallationKeyReference | None:
        return None

    def verified_grants(self) -> tuple[VerifiedGrantReference, ...]:
        return ()


def _plain_database(
    path: Path,
    *,
    tables: tuple[str, ...] = REQUIRED_TABLES,
    unique: bool = True,
    pragmas: dict[str, str] | None = None,
) -> PlainDatabase:
    connection = plaintext_sqlite.connect(path)
    try:
        for table in tables:
            column = UNIQUE_DIGEST_COLUMNS.get(table)
            if column is None:
                connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                continue
            constraint = " UNIQUE" if unique else ""
            connection.execute(
                f"CREATE TABLE {table} ("
                f"id INTEGER PRIMARY KEY, {column} TEXT NOT NULL{constraint})"
            )
        connection.commit()
    finally:
        connection.close()
    return PlainDatabase(
        path,
        pragmas
        or {"journal_mode": "wal", "synchronous": "FULL", "foreign_keys": "ON"},
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _discard_recorded_activation() -> Iterator[None]:
    reset_activation()
    yield
    reset_activation()


@pytest.fixture
def now() -> datetime:
    return VECTOR_INSTANT


@pytest.fixture
def installation_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def key_provider(
    installation_private_key: ec.EllipticCurvePrivateKey,
) -> EmulatedKeyProvider:
    return EmulatedKeyProvider(installation_private_key.public_key())


def _provisioned_store(
    path: Path,
    now: datetime,
    installation_private_key: ec.EllipticCurvePrivateKey,
    grant_types: tuple[str, ...],
) -> SQLiteAuthenticationStore:
    """Build a store with an activated installation key and the named grants."""

    store = SQLiteAuthenticationStore(path)
    created_at = now - timedelta(days=1)
    store.reserve_installation_key(
        InstallationKeyReservation(
            PLATFORM_CRYPTO_PROVIDER,
            PROVIDER_KEY_NAME,
            INSTALLATION_KEY_ALGORITHM,
            created_at,
        )
    )
    numbers = installation_private_key.public_key().public_numbers()
    store.activate_installation_key(
        InstallationKeyReference(
            provider_name=PLATFORM_CRYPTO_PROVIDER,
            provider_key_name=PROVIDER_KEY_NAME,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            installation_key_id=INSTALLATION_KEY_ID,
            installation_key_jkt=INSTALLATION_KEY_JKT,
            public_key_jwk=json.dumps(
                {
                    "crv": "P-256",
                    "kid": INSTALLATION_KEY_ID,
                    "kty": "EC",
                    "x": numbers.x.to_bytes(32, "big").hex(),
                    "y": numbers.y.to_bytes(32, "big").hex(),
                },
                sort_keys=True,
            ),
            created_at=created_at,
            activated_at=created_at,
        )
    )
    for grant_type in grant_types:
        store.record_verified_grant(
            _admit(grant_type, "valid-current", verified_at=now - timedelta(minutes=1))
        )
    return store


@pytest.fixture
def store(
    tmp_path: Path,
    now: datetime,
    installation_private_key: ec.EllipticCurvePrivateKey,
) -> SQLiteAuthenticationStore:
    """A provisioned store: an activated installation key and every grant."""

    return _provisioned_store(
        tmp_path / "auth.sqlite3", now, installation_private_key, RECORDED_GRANT_TYPES
    )


@pytest.fixture
def unauthorized_store(
    tmp_path: Path,
    now: datetime,
    installation_private_key: ec.EllipticCurvePrivateKey,
) -> SQLiteAuthenticationStore:
    """An installation during setup: valid identity and no account grant."""

    return _provisioned_store(
        tmp_path / "setup" / "auth.sqlite3",
        now,
        installation_private_key,
        ACTIVATION_GRANT_TYPES,
    )


@pytest.fixture
def configuration() -> RuntimeConfiguration:
    return RuntimeConfiguration(
        environment="production",
        deployable=True,
        websocket_auth_mode="local_session",
        websocket_bind_host="127.0.0.1",
        bridge_origin="http://bridge.localhost:17871",
        identity_binding_source="verified_grants",
        principal_id="principal-fixture-001",
    )


@pytest.fixture
def inputs(
    configuration: RuntimeConfiguration,
    store: SQLiteAuthenticationStore,
    key_provider: EmulatedKeyProvider,
    now: datetime,
) -> ActivationInputs:
    return ActivationInputs(
        configuration=configuration,
        store=store,
        key_provider=key_provider,
        now=now,
    )


def _refusal(decision: ActivationDecision, condition: str) -> str:
    detail = {
        outcome.condition: outcome.detail for outcome in decision.refusals
    }.get(condition)
    assert detail is not None, (
        f"{condition} was satisfied; refused: {decision.refused_conditions}"
    )
    return detail


# --------------------------------------------------------------------------
# The satisfied baseline
# --------------------------------------------------------------------------


@pytest.mark.contract_integrity
def test_a_provisioned_production_runtime_activates(
    inputs: ActivationInputs,
) -> None:
    decision = evaluate_activation(inputs)

    assert decision.production
    assert decision.activated, decision.refusals
    assert [outcome.condition for outcome in decision.outcomes] == [
        DEVELOPMENT_AUTHENTICATION,
        PINNED_SIGNING_TRUST,
        REQUIRED_GRANTS,
        AUTHENTICATION_STORE,
        NON_EXPORTABLE_INSTALLATION_KEY,
        LOOPBACK_EXPOSURE,
    ]


def test_a_development_runtime_is_outside_the_production_posture(
    inputs: ActivationInputs,
) -> None:
    decision = evaluate_activation(
        replace(
            inputs,
            configuration=replace(
                inputs.configuration, environment="development", deployable=False
            ),
        )
    )

    assert decision.activated
    assert not decision.production
    assert decision.outcomes == ()


# --------------------------------------------------------------------------
# The decisive cases
# --------------------------------------------------------------------------


@pytest.mark.contract_integrity
def test_activation_refuses_a_tampered_grant(
    inputs: ActivationInputs, now: datetime
) -> None:
    """A tampered grant is never admitted, so activation has no valid reference."""

    store = inputs.store
    assert isinstance(store, SQLiteAuthenticationStore)

    with pytest.raises(GrantAdmissionRefused) as refusal:
        _admit("installation_grant", "tampered-payload", verified_at=now)
    decision = evaluate_activation(
        replace(inputs, store=_StaticStore(store, _without(store, "installation_grant")))
    )

    assert str(refusal.value) == "invalid_signature"
    assert not decision.activated
    assert REQUIRED_GRANTS in decision.refused_conditions
    assert (
        _refusal(decision, REQUIRED_GRANTS)
        == "no installation_grant reference is inside its validity boundary"
    )


@pytest.mark.contract_integrity
def test_activation_refuses_a_fixture_trust_set(inputs: ActivationInputs) -> None:
    """Every trust set except the production one is refused outside development."""

    fixture = evaluate_activation(
        replace(
            inputs,
            configuration=replace(
                inputs.configuration, trust_set_path=FIXTURE_TRUST_SET_PATH
            ),
        )
    )
    production = evaluate_activation(
        replace(
            inputs,
            configuration=replace(
                inputs.configuration, trust_set_path=PRODUCTION_TRUST_SET_PATH
            ),
        )
    )

    assert not fixture.activated
    assert "not production usable" in _refusal(fixture, PINNED_SIGNING_TRUST)
    assert production.activated, production.refusals


# --------------------------------------------------------------------------
# Condition: development authentication in a deployable runtime
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"websocket_auth_mode": "development_stub"}, "authentication selector is"),
        ({"environment": "development"}, "deployable runtime declares environment"),
        ({"identity_binding_source": "development"}, "identity binding source is"),
        ({"principal_id": DEVELOPMENT_PRINCIPAL_ID}, "fixed development mapping"),
    ],
)
def test_development_authentication_in_a_deployable_runtime_refuses(
    inputs: ActivationInputs, overrides: dict[str, Any], expected: str
) -> None:
    decision = evaluate_activation(
        replace(inputs, configuration=replace(inputs.configuration, **overrides))
    )

    assert not decision.activated
    assert expected in _refusal(decision, DEVELOPMENT_AUTHENTICATION)


def test_the_gate_names_the_transport_development_identities() -> None:
    assert DEVELOPMENT_PRINCIPAL_ID == DEV_PRINCIPAL_ID


def test_activation_reads_no_creator_account() -> None:
    """Installation activation never depends on an authorized account."""

    declared = {
        "environment",
        "deployable",
        "websocket_auth_mode",
        "websocket_bind_host",
        "bridge_origin",
        "identity_binding_source",
        "principal_id",
        "trust_set_path",
    }
    present = {field.name for field in fields(RuntimeConfiguration)}

    assert present == declared
    assert DEV_ACCOUNT_ID not in str(runtime_configuration())


# --------------------------------------------------------------------------
# Condition: pinned signing trust
# --------------------------------------------------------------------------


@pytest.mark.contract_integrity
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("production/grant-profile-v1/absent.json", "unusable"),
        ("capability-permit-v1/trust-set.json", "not production usable"),
    ],
)
def test_pinned_signing_trust_refuses_an_unpinned_or_replaced_set(
    inputs: ActivationInputs, path: str, expected: str
) -> None:
    decision = evaluate_activation(
        replace(inputs, configuration=replace(inputs.configuration, trust_set_path=path))
    )

    assert not decision.activated
    assert expected in _refusal(decision, PINNED_SIGNING_TRUST)


# --------------------------------------------------------------------------
# Condition: required grants
# --------------------------------------------------------------------------


class _StaticStore:
    """Authentication store view returning a fixed set of grant references."""

    def __init__(
        self,
        source: SQLiteAuthenticationStore,
        grants: tuple[VerifiedGrantReference, ...],
    ) -> None:
        self.database = source.database
        self._reference = source.installation_key_reference()
        self._grants = grants

    def installation_key_reference(self) -> InstallationKeyReference | None:
        return self._reference

    def verified_grants(self) -> tuple[VerifiedGrantReference, ...]:
        return self._grants


def _without(
    store: SQLiteAuthenticationStore, grant_type: str
) -> tuple[VerifiedGrantReference, ...]:
    return tuple(
        reference
        for reference in store.verified_grants()
        if reference.grant_type != grant_type
    )


def _replacing(
    store: SQLiteAuthenticationStore, grant_type: str, **changes: Any
) -> tuple[VerifiedGrantReference, ...]:
    return tuple(
        replace(reference, **changes) if reference.grant_type == grant_type else reference
        for reference in store.verified_grants()
    )


@pytest.mark.contract_integrity
@pytest.mark.parametrize("grant_type", ACTIVATION_GRANT_TYPES)
def test_required_grants_refuse_an_absent_grant(
    inputs: ActivationInputs, grant_type: str
) -> None:
    store = inputs.store
    assert isinstance(store, SQLiteAuthenticationStore)

    decision = evaluate_activation(
        replace(inputs, store=_StaticStore(store, _without(store, grant_type)))
    )

    assert not decision.activated
    assert (
        _refusal(decision, REQUIRED_GRANTS)
        == f"no {grant_type} reference is inside its validity boundary"
    )


@pytest.mark.contract_integrity
def test_required_grants_refuse_a_grant_outside_its_validity_boundary(
    inputs: ActivationInputs, now: datetime
) -> None:
    store = inputs.store
    assert isinstance(store, SQLiteAuthenticationStore)

    decision = evaluate_activation(
        replace(
            inputs,
            store=_StaticStore(
                store,
                _replacing(store, "membership_snapshot", expires_at=now),
            ),
        )
    )

    assert not decision.activated
    assert (
        _refusal(decision, REQUIRED_GRANTS)
        == "no membership_snapshot reference is inside its validity boundary"
    )


@pytest.mark.contract_integrity
def test_required_grants_refuse_a_foreign_installation_key_binding(
    inputs: ActivationInputs,
) -> None:
    store = inputs.store
    assert isinstance(store, SQLiteAuthenticationStore)

    decision = evaluate_activation(
        replace(
            inputs,
            store=_StaticStore(
                store,
                _replacing(
                    store, "installation_grant", installation_key_id="ik1.other"
                ),
            ),
        )
    )

    assert not decision.activated
    assert (
        _refusal(decision, REQUIRED_GRANTS)
        == "installation_grant is not bound to the active installation key"
    )


@pytest.mark.contract_integrity
def test_required_grants_refuse_a_mismatched_installation(
    inputs: ActivationInputs,
) -> None:
    store = inputs.store
    assert isinstance(store, SQLiteAuthenticationStore)

    decision = evaluate_activation(
        replace(
            inputs,
            store=_StaticStore(
                store,
                _replacing(
                    store, "membership_snapshot", installation_id="other-installation"
                ),
            ),
        )
    )

    assert not decision.activated
    assert _refusal(decision, REQUIRED_GRANTS) == "required grants name 2 installations"


@pytest.mark.contract_integrity
def test_required_grants_refuse_a_mismatched_external_principal(
    inputs: ActivationInputs,
) -> None:
    store = inputs.store
    assert isinstance(store, SQLiteAuthenticationStore)

    decision = evaluate_activation(
        replace(
            inputs,
            store=_StaticStore(
                store, _replacing(store, "membership_snapshot", subject="other-subject")
            ),
        )
    )

    assert not decision.activated
    assert (
        _refusal(decision, REQUIRED_GRANTS)
        == "required grants name 2 external principals"
    )


def test_required_grants_refuse_an_absent_store(inputs: ActivationInputs) -> None:
    decision = evaluate_activation(replace(inputs, store=None))

    assert not decision.activated
    assert _refusal(decision, REQUIRED_GRANTS) == "the authentication store is unavailable"


# --------------------------------------------------------------------------
# Activation is separate from account authorization
# --------------------------------------------------------------------------


def _authorize_account_on(
    store: SQLiteAuthenticationStore, now: datetime
) -> AuthorizedAccountBinding:
    """Record one authorized account whose basis is the installation grants.

    The writer applies cardinality and referential integrity, not grant-type
    composition, so this state is reachable. It exists to leave the resolver's
    coherence check as the only thing that can withhold the account.
    """

    identity = store.verified_grants()[0]
    association_request_id = "association-setup"
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=association_request_id,
            installation_id=identity.installation_id,
            onboarding_transaction_id="onboarding-setup",
            organization_id=identity.organization_id,
            creator_account_id=ACCOUNT_ID,
            state=ProvisioningCandidateState.PENDING,
            requested_at=now - timedelta(minutes=3),
        )
    )
    store.approve_provisioning_candidate(
        association_request_id, resolved_at=now - timedelta(minutes=2)
    )
    binding = AuthorizedAccountBinding(
        creator_account_id=ACCOUNT_ID,
        installation_id=identity.installation_id,
        platform_creator_id=ACCOUNT_ID,
        association_request_id=association_request_id,
        grant_bundle_sha256="ab" * 32,
        authorized_at=now - timedelta(minutes=1),
        grant_reference_ids=tuple(
            grant.reference_id for grant in store.verified_grants()
        ),
    )
    store.record_authorized_account_binding(binding)
    return binding


@pytest.mark.contract_integrity
def test_an_installation_with_no_authorized_account_activates(
    inputs: ActivationInputs,
    unauthorized_store: SQLiteAuthenticationStore,
    now: datetime,
) -> None:
    """Setup state: valid identity, no account authorized, runtime activated."""

    decision = evaluate_activation(replace(inputs, store=unauthorized_store))

    assert decision.activated, decision.refusals
    assert unauthorized_store.authorized_account_bindings() == ()
    assert eligible_accounts(unauthorized_store, now=now) == ()


@pytest.mark.contract_integrity
def test_an_active_installation_is_not_authority_for_an_account(
    inputs: ActivationInputs,
    unauthorized_store: SQLiteAuthenticationStore,
    now: datetime,
) -> None:
    """Being active must not imply authority to act for any account.

    The binding is recorded and unrevoked, its account scope is unrevoked, and
    the grants it rests on are current -- activation succeeding on the same
    store is what establishes that they are. Every other reason the resolver
    could withhold the account is therefore excluded, and only the coherence
    check between the account binding and the membership snapshot remains.
    """

    binding = _authorize_account_on(unauthorized_store, now)
    decision = evaluate_activation(replace(inputs, store=unauthorized_store))

    assert decision.activated, decision.refusals
    assert [
        (record.creator_account_id, record.revoked_at)
        for record in unauthorized_store.authorized_account_bindings()
    ] == [(ACCOUNT_ID, None)]
    assert (
        unauthorized_store.scope_is_revoked(
            RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, ACCOUNT_ID)
        )
        is False
    )
    assert binding.grant_reference_ids != ()

    assert eligible_accounts(unauthorized_store, now=now) == ()
    with pytest.raises(AccountResolutionRefused) as sole:
        sole_eligible_account(unauthorized_store, now=now)
    with pytest.raises(AccountResolutionRefused) as scoped:
        eligible_account_for_policy(
            unauthorized_store,
            unauthorized_store.build_runtime_policy(
                AuthContext(
                    principal_id="principal-fixture-001",
                    creator_account_id=ACCOUNT_ID,
                    role="creator",
                )
            ),
            now=now,
        )

    assert sole.value.reason == "no_eligible_account"
    assert scoped.value.reason == "no_eligible_account"


# --------------------------------------------------------------------------
# Condition: durable authentication store
# --------------------------------------------------------------------------


@pytest.mark.contract_integrity
def test_the_provisioned_store_satisfies_the_durable_store_condition(
    inputs: ActivationInputs,
) -> None:
    decision = evaluate_activation(inputs)

    assert AUTHENTICATION_STORE not in decision.refused_conditions


def test_a_stand_in_store_with_the_required_shape_is_accepted(
    inputs: ActivationInputs, tmp_path: Path
) -> None:
    """The deviating stand-ins below are refused for their deviation, not their shape."""

    database = _plain_database(tmp_path / "shaped.sqlite3")

    decision = evaluate_activation(replace(inputs, store=PlainStore(database)))

    assert AUTHENTICATION_STORE not in decision.refused_conditions


@pytest.mark.parametrize(
    ("pragmas", "expected"),
    [
        (
            {"journal_mode": "delete", "synchronous": "FULL", "foreign_keys": "ON"},
            "journal mode is delete",
        ),
        (
            {"journal_mode": "wal", "synchronous": "NORMAL", "foreign_keys": "ON"},
            "not synchronous=FULL",
        ),
        (
            {"journal_mode": "wal", "synchronous": "FULL", "foreign_keys": "OFF"},
            "foreign keys disabled",
        ),
    ],
)
def test_the_store_condition_refuses_a_database_without_durable_pragmas(
    inputs: ActivationInputs,
    tmp_path: Path,
    pragmas: dict[str, str],
    expected: str,
) -> None:
    database = _plain_database(
        tmp_path / f"{pragmas['journal_mode']}-{pragmas['synchronous']}-{pragmas['foreign_keys']}.sqlite3",
        pragmas=pragmas,
    )

    decision = evaluate_activation(replace(inputs, store=PlainStore(database)))

    assert not decision.activated
    assert expected in _refusal(decision, AUTHENTICATION_STORE)


def test_the_store_condition_refuses_a_database_missing_a_required_table(
    inputs: ActivationInputs, tmp_path: Path
) -> None:
    database = _plain_database(
        tmp_path / "incomplete.sqlite3",
        tables=tuple(table for table in REQUIRED_TABLES if table != "runtime_tickets"),
    )

    decision = evaluate_activation(replace(inputs, store=PlainStore(database)))

    assert not decision.activated
    assert "missing ['runtime_tickets']" in _refusal(decision, AUTHENTICATION_STORE)


def test_the_store_condition_refuses_a_database_without_atomic_consumption(
    inputs: ActivationInputs, tmp_path: Path
) -> None:
    database = _plain_database(tmp_path / "nonunique.sqlite3", unique=False)

    decision = evaluate_activation(replace(inputs, store=PlainStore(database)))

    assert not decision.activated
    assert "does not constrain auth_challenges(secret_digest) uniquely" in _refusal(
        decision, AUTHENTICATION_STORE
    )


def test_the_store_condition_refuses_a_database_that_is_not_a_file(
    inputs: ActivationInputs, tmp_path: Path
) -> None:
    database = _plain_database(tmp_path / "absent.sqlite3")
    database.path.unlink()

    decision = evaluate_activation(replace(inputs, store=PlainStore(database)))

    assert not decision.activated
    assert (
        _refusal(decision, AUTHENTICATION_STORE)
        == "the authentication database is not a durable file"
    )


# --------------------------------------------------------------------------
# Condition: non-exportable installation key
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"export_policy": 1}, "permits private-key export"),
        ({"hardware_backed": False}, "not hardware-backed"),
        ({"present": False}, "absent from its provider"),
        (
            {"provider_name": "Microsoft Software Key Storage Provider"},
            "installation key provider is Microsoft Software Key Storage Provider",
        ),
    ],
)
def test_the_installation_key_condition_refuses_an_unprotected_key(
    inputs: ActivationInputs,
    installation_private_key: ec.EllipticCurvePrivateKey,
    overrides: dict[str, Any],
    expected: str,
) -> None:
    provider = EmulatedKeyProvider(
        installation_private_key.public_key(), **overrides
    )

    decision = evaluate_activation(replace(inputs, key_provider=provider))

    assert not decision.activated
    assert expected in _refusal(decision, NON_EXPORTABLE_INSTALLATION_KEY)


def test_the_installation_key_condition_refuses_an_absent_provider(
    inputs: ActivationInputs,
) -> None:
    decision = evaluate_activation(replace(inputs, key_provider=None))

    assert not decision.activated
    assert (
        _refusal(decision, NON_EXPORTABLE_INSTALLATION_KEY)
        == "the installation key provider is unavailable"
    )


def test_the_installation_key_condition_refuses_an_unprovisioned_installation(
    inputs: ActivationInputs, tmp_path: Path
) -> None:
    empty = SQLiteAuthenticationStore(tmp_path / "empty" / "auth.sqlite3")

    decision = evaluate_activation(replace(inputs, store=empty))

    assert not decision.activated
    assert (
        _refusal(decision, NON_EXPORTABLE_INSTALLATION_KEY)
        == "the installation key reference is absent"
    )


# --------------------------------------------------------------------------
# Condition: loopback exposure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"websocket_bind_host": "0.0.0.0"}, "non-loopback hosts ['0.0.0.0']"),
        (
            {"bridge_origin": "https://analytics.example.com"},
            "non-loopback hosts ['analytics.example.com']",
        ),
        ({"bridge_origin": "bridge.localhost:17871"}, "is not an origin"),
    ],
)
def test_the_exposure_condition_refuses_a_non_loopback_runtime(
    inputs: ActivationInputs, overrides: dict[str, Any], expected: str
) -> None:
    decision = evaluate_activation(
        replace(inputs, configuration=replace(inputs.configuration, **overrides))
    )

    assert not decision.activated
    assert expected in _refusal(decision, LOOPBACK_EXPOSURE)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_the_exposure_condition_accepts_every_loopback_bind_host(
    inputs: ActivationInputs, host: str
) -> None:
    decision = evaluate_activation(
        replace(
            inputs, configuration=replace(inputs.configuration, websocket_bind_host=host)
        )
    )

    assert LOOPBACK_EXPOSURE not in decision.refused_conditions


# --------------------------------------------------------------------------
# Undetermined conditions
# --------------------------------------------------------------------------


def test_a_condition_that_cannot_be_determined_refuses_activation(
    inputs: ActivationInputs,
) -> None:
    store = inputs.store
    assert isinstance(store, SQLiteAuthenticationStore)
    reference = store.installation_key_reference()
    assert reference is not None

    decision = evaluate_activation(replace(inputs, store=UnreadableStore(reference)))

    assert not decision.activated
    assert _refusal(decision, REQUIRED_GRANTS) == "undetermined: OperationalError"


# --------------------------------------------------------------------------
# Activation is separate from process startup
# --------------------------------------------------------------------------


def _refused_decision() -> ActivationDecision:
    return ActivationDecision(
        activated=False,
        production=True,
        outcomes=(
            ConditionOutcome(
                REQUIRED_GRANTS,
                False,
                "no installation_grant reference is inside its validity boundary",
            ),
        ),
    )


def test_a_refused_activation_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    refused = _refused_decision()
    monkeypatch.setattr(main_module, "evaluate_runtime_activation", lambda: refused)

    main_module.activate_runtime()

    assert not runtime_is_activated()


def test_an_unavailable_key_provider_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    def unavailable() -> None:
        raise InstallationKeyUnavailable("provider probe failed")

    monkeypatch.setattr(main_module.settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(main_module, "initialize_installation_key", unavailable)
    monkeypatch.setattr(main_module, "evaluate_runtime_activation", _refused_decision)

    main_module.activate_runtime()

    assert not runtime_is_activated()


def test_an_installation_key_policy_refusal_stops_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    def refused() -> None:
        raise InstallationKeyPolicyError(
            "The installation key permits private-key export"
        )

    monkeypatch.setattr(main_module.settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(main_module, "initialize_installation_key", refused)

    with pytest.raises(InstallationKeyPolicyError, match="permits private-key export"):
        main_module.activate_runtime()


def test_an_unactivated_runtime_still_serves_the_provisioning_surface() -> None:
    import app.main as main_module

    record_activation(_refused_decision())
    client = TestClient(main_module.app)

    health = client.get("/health")
    gated = client.get("/api/v1/settings/history")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert gated.status_code == 503
    assert gated.json()["detail"] == "The local runtime is not activated"


def test_an_unactivated_runtime_refuses_a_runtime_route() -> None:
    from app.api.activation import require_activated_runtime

    record_activation(_refused_decision())

    with pytest.raises(HTTPException) as refusal:
        require_activated_runtime()

    assert refusal.value.status_code == 503
    assert refusal.value.detail == "The local runtime is not activated"


def test_an_activated_runtime_admits_a_runtime_route(
    inputs: ActivationInputs,
) -> None:
    from app.api.activation import require_activated_runtime

    record_activation(evaluate_activation(inputs))

    require_activated_runtime()


def test_an_unactivated_runtime_refuses_transport_authentication() -> None:
    record_activation(_refused_decision())

    with pytest.raises(AuthenticationError) as refusal:
        transport_manager.authenticate(
            DEV_BRIDGE_AUTH_TICKET, DEV_ACCOUNT_ID, role="bridge"
        )

    assert str(refusal.value) == "The local runtime is not activated"


def test_an_activated_runtime_admits_transport_authentication(
    inputs: ActivationInputs,
) -> None:
    record_activation(evaluate_activation(inputs))

    principal_id, account_id = transport_manager.authenticate(
        DEV_BRIDGE_AUTH_TICKET, DEV_ACCOUNT_ID, role="bridge"
    )

    assert (principal_id, account_id) == (DEVELOPMENT_PRINCIPAL_ID, DEV_ACCOUNT_ID)
