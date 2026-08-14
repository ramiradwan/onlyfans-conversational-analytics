"""Startup conditions for local runtime activation.

Activation is separate from process startup. An installation that does not
satisfy every condition still starts and exposes the bounded loopback
provisioning surface; it does not activate the authorized runtime.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable
from urllib.parse import urlsplit

from contracts.loader import ContractsIntegrityError

from app.core.config import settings
from app.core.runtime_paths import runtime_configuration_file
from app.persistence.auth import (
    AuthenticationStore,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.persistence.database import SQLiteConfigurationError
from app.security.grant_verifier import load_pinned_trust_set, trusted_signing_keys
from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    PLATFORM_CRYPTO_PROVIDER,
    InstallationKeyProvider,
    WindowsCNGInstallationKeyProvider,
)


DEVELOPMENT_AUTHENTICATION = "development_authentication"
PINNED_SIGNING_TRUST = "pinned_signing_trust"
REQUIRED_GRANTS = "required_grants"
AUTHENTICATION_STORE = "authentication_store"
NON_EXPORTABLE_INSTALLATION_KEY = "non_exportable_installation_key"
LOOPBACK_EXPOSURE = "loopback_exposure"

PRODUCTION_TRUST_SET_PATH = "production/grant-profile-v1/trust-set.json"
GRANT_PROFILE = "urn:bridge-clean:grant-profile:v1"
DEVELOPMENT_PRINCIPAL_ID = "dev-principal"

_DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "dev", "local", "test"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_REQUIRED_GRANT_TYPES = (
    "installation_grant",
    "membership_snapshot",
    "creator_account_binding",
)
_REQUIRED_TRUST_PURPOSES = frozenset({"installation-binding", "membership"})
_REQUIRED_TABLES = frozenset(
    {
        "agent_pairings",
        "auth_challenges",
        "auth_revocation_bindings",
        "auth_revocation_state",
        "authorization_epoch",
        "bridge_sessions",
        "installation_key_reference",
        "runtime_tickets",
        "verified_grant_references",
    }
)
_REQUIRED_UNIQUE_COLUMNS = (
    ("auth_challenges", ("secret_digest",)),
    ("runtime_tickets", ("secret_digest",)),
    ("verified_grant_references", ("grant_digest",)),
)


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """Configuration values one activation decision reads."""

    environment: str
    deployable: bool
    websocket_auth_mode: str
    websocket_bind_host: str
    bridge_origin: str
    identity_binding_source: str
    principal_id: str
    trust_set_path: str = PRODUCTION_TRUST_SET_PATH

    @property
    def production(self) -> bool:
        """Report whether the activation conditions apply to this runtime."""

        return (
            self.deployable
            or self.environment.strip().lower() not in _DEVELOPMENT_ENVIRONMENTS
        )


@dataclass(frozen=True, slots=True)
class ActivationInputs:
    """Live state one activation decision reads."""

    configuration: RuntimeConfiguration
    store: AuthenticationStore | None
    key_provider: InstallationKeyProvider | None
    now: datetime


@dataclass(frozen=True, slots=True)
class ConditionOutcome:
    condition: str
    satisfied: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    """The outcome of one evaluation of the activation conditions."""

    activated: bool
    production: bool
    outcomes: tuple[ConditionOutcome, ...] = ()

    @property
    def refusals(self) -> tuple[ConditionOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.satisfied)

    @property
    def refused_conditions(self) -> tuple[str, ...]:
        return tuple(outcome.condition for outcome in self.refusals)


def _development_authentication(inputs: ActivationInputs) -> str | None:
    configuration = inputs.configuration
    if configuration.websocket_auth_mode != "local_session":
        return f"authentication selector is {configuration.websocket_auth_mode}"
    if configuration.environment.strip().lower() in _DEVELOPMENT_ENVIRONMENTS:
        return f"deployable runtime declares environment {configuration.environment}"
    if configuration.identity_binding_source != "verified_grants":
        return f"identity binding source is {configuration.identity_binding_source}"
    if configuration.principal_id == DEVELOPMENT_PRINCIPAL_ID:
        return (
            "runtime identity uses the fixed development mapping "
            f"['{DEVELOPMENT_PRINCIPAL_ID}']"
        )
    return None


def _pinned_signing_trust(inputs: ActivationInputs) -> str | None:
    path = inputs.configuration.trust_set_path
    try:
        trust_set = load_pinned_trust_set(path)
    except ContractsIntegrityError as error:
        return f"pinned trust set is unusable: {error}"
    if trust_set.get("profile") != GRANT_PROFILE:
        return f"pinned trust set declares profile {trust_set.get('profile')!r}"
    sequence = trust_set.get("transition_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        return "pinned trust set has no positive transition sequence"
    try:
        purposes = set(trusted_signing_keys(trust_set).values())
    except ValueError as error:
        return f"pinned trust set has no usable signing key: {error}"
    missing = sorted(_REQUIRED_TRUST_PURPOSES - purposes)
    if missing:
        return f"pinned trust set is missing signing purposes {missing}"
    return None


def _required_grants(inputs: ActivationInputs) -> str | None:
    store = inputs.store
    if store is None:
        return "the authentication store is unavailable"
    key = store.installation_key_reference()
    if key is None:
        return "the installation key reference is absent"
    grants = store.verified_grants()
    selected: dict[str, VerifiedGrantReference] = {}
    for grant_type in _REQUIRED_GRANT_TYPES:
        current = [
            grant
            for grant in grants
            if grant.grant_type == grant_type
            and grant.valid_from <= inputs.now < grant.expires_at
        ]
        if not current:
            return f"no {grant_type} reference is inside its validity boundary"
        selected[grant_type] = max(current, key=lambda grant: grant.verified_at)
    for grant_type, grant in selected.items():
        if (
            grant.installation_key_id != key.installation_key_id
            or grant.installation_key_jkt != key.installation_key_jkt
        ):
            return f"{grant_type} is not bound to the active installation key"
    installations = {grant.installation_id for grant in selected.values()}
    if len(installations) != 1:
        return f"required grants name {len(installations)} installations"
    principals = {(grant.issuer, grant.subject) for grant in selected.values()}
    if len(principals) != 1:
        return f"required grants name {len(principals)} external principals"
    # The account is read from the grant that carries it, never from
    # installation configuration; this checks the grant set is self-coherent.
    account_id = selected["creator_account_binding"].creator_account_id
    if not account_id:
        return "the account-binding grant names no creator account"
    allowed = selected["membership_snapshot"].allowed_creator_account_ids
    if allowed is None or account_id not in allowed:
        return "the membership grant does not allow the bound creator account"
    return None


def _authentication_store(inputs: ActivationInputs) -> str | None:
    store = inputs.store
    if store is None:
        return "the authentication store is unavailable"
    database = getattr(store, "database", None)
    if database is None:
        return "the authentication store has no durable database"
    if not Path(database.path).is_file():
        return "the authentication database is not a durable file"
    with database.read() as connection:
        journal_mode = str(_pragma(connection, "journal_mode")).lower()
        if journal_mode != "wal":
            return f"the authentication database journal mode is {journal_mode}"
        if int(_pragma(connection, "synchronous")) != 2:
            return "the authentication database is not synchronous=FULL"
        if int(_pragma(connection, "foreign_keys")) != 1:
            return "the authentication database has foreign keys disabled"
        missing = sorted(_REQUIRED_TABLES - _table_names(connection))
        if missing:
            return f"the authentication database is missing {missing}"
        for table, columns in _REQUIRED_UNIQUE_COLUMNS:
            if columns not in _unique_column_sets(connection, table):
                return (
                    f"the authentication database does not constrain "
                    f"{table}({', '.join(columns)}) uniquely"
                )
    try:
        database.validate_integrity()
    except SQLiteConfigurationError as error:
        return f"the authentication database failed its integrity check: {error}"
    return None


def _non_exportable_installation_key(inputs: ActivationInputs) -> str | None:
    store = inputs.store
    if store is None:
        return "the authentication store is unavailable"
    provider = inputs.key_provider
    if provider is None:
        return "the installation key provider is unavailable"
    if provider.provider_name != PLATFORM_CRYPTO_PROVIDER:
        return f"the installation key provider is {provider.provider_name}"
    reference = store.installation_key_reference()
    if reference is None:
        return "the installation key reference is absent"
    if reference.provider_name != PLATFORM_CRYPTO_PROVIDER:
        return f"the installation key names provider {reference.provider_name}"
    if reference.algorithm != INSTALLATION_KEY_ALGORITHM:
        return f"the installation key algorithm is {reference.algorithm}"
    info = provider.key_info(reference.provider_key_name)
    if info is None:
        return "the installation key is absent from its provider"
    if info.provider_name != PLATFORM_CRYPTO_PROVIDER:
        return f"the installation key resides in provider {info.provider_name}"
    if not info.hardware_backed:
        return "the installation key is not hardware-backed"
    if info.export_policy != 0:
        return "the installation key permits private-key export"
    return None


def _loopback_exposure(inputs: ActivationInputs) -> str | None:
    configuration = inputs.configuration
    origin = urlsplit(configuration.bridge_origin)
    if origin.scheme not in {"http", "https"} or not origin.hostname:
        return f"the bridge origin {configuration.bridge_origin!r} is not an origin"
    exposed = sorted(
        host
        for host in (configuration.websocket_bind_host, origin.hostname)
        if not _is_loopback(host)
    )
    if exposed:
        return f"the runtime is exposed on non-loopback hosts {exposed}"
    return None


_CONDITION_CHECKS: tuple[tuple[str, Callable[[ActivationInputs], str | None]], ...] = (
    (DEVELOPMENT_AUTHENTICATION, _development_authentication),
    (PINNED_SIGNING_TRUST, _pinned_signing_trust),
    (REQUIRED_GRANTS, _required_grants),
    (AUTHENTICATION_STORE, _authentication_store),
    (NON_EXPORTABLE_INSTALLATION_KEY, _non_exportable_installation_key),
    (LOOPBACK_EXPOSURE, _loopback_exposure),
)


def evaluate_activation(inputs: ActivationInputs) -> ActivationDecision:
    """Evaluate every activation condition against the supplied runtime state."""

    if not inputs.configuration.production:
        return ActivationDecision(activated=True, production=False)
    outcomes: list[ConditionOutcome] = []
    for condition, check in _CONDITION_CHECKS:
        try:
            detail = check(inputs)
        except Exception as error:
            # A condition that cannot be determined refuses activation.
            detail = f"undetermined: {type(error).__name__}"
        outcomes.append(
            ConditionOutcome(condition, detail is None, detail or "satisfied")
        )
    return ActivationDecision(
        activated=all(outcome.satisfied for outcome in outcomes),
        production=True,
        outcomes=tuple(outcomes),
    )


def runtime_configuration() -> RuntimeConfiguration:
    """Read the activation configuration from live runtime settings."""

    return RuntimeConfiguration(
        environment=settings.environment,
        deployable=_runtime_configuration_file_present(),
        websocket_auth_mode=settings.websocket_auth_mode,
        websocket_bind_host=settings.websocket_bind_host,
        bridge_origin=settings.bridge_origin,
        identity_binding_source=settings.identity_binding_source,
        principal_id=settings.local_principal_id,
    )


def evaluate_runtime_activation() -> ActivationDecision:
    """Evaluate the activation conditions against live runtime state."""

    configuration = runtime_configuration()
    if not configuration.production:
        return ActivationDecision(activated=True, production=False)
    return evaluate_activation(
        ActivationInputs(
            configuration=configuration,
            store=_open_authentication_store(),
            key_provider=_open_installation_key_provider(),
            now=datetime.now(timezone.utc),
        )
    )


_activation: ActivationDecision | None = None
_activation_lock = RLock()


def current_activation() -> ActivationDecision:
    """Return the recorded decision, evaluating live state on first use."""

    global _activation
    with _activation_lock:
        if _activation is None:
            _activation = evaluate_runtime_activation()
        return _activation


def record_activation(decision: ActivationDecision) -> ActivationDecision:
    """Record the decision one startup produced."""

    global _activation
    with _activation_lock:
        _activation = decision
        return decision


def reset_activation() -> None:
    """Discard the recorded decision so the next read re-evaluates."""

    global _activation
    with _activation_lock:
        _activation = None


def runtime_is_activated() -> bool:
    return current_activation().activated


def _runtime_configuration_file_present() -> bool:
    try:
        return runtime_configuration_file().exists()
    except (OSError, ValueError):
        # An unreadable installation location is treated as deployable.
        return True


def _open_authentication_store() -> AuthenticationStore | None:
    try:
        return SQLiteAuthenticationStore(settings.auth_database_path)
    except Exception:
        return None


def _open_installation_key_provider() -> InstallationKeyProvider | None:
    try:
        return WindowsCNGInstallationKeyProvider()
    except Exception:
        return None


def _pragma(connection: sqlite3.Connection, name: str) -> object:
    return connection.execute(f"PRAGMA {name}").fetchone()[0]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _unique_column_sets(
    connection: sqlite3.Connection, table: str
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if not index["unique"] or index["partial"]:
            continue
        columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f"PRAGMA index_info({index['name']})"
            ).fetchall()
        )
        result.add(columns)
    return result


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    return normalized in _LOOPBACK_HOSTS or normalized.endswith(".localhost")
