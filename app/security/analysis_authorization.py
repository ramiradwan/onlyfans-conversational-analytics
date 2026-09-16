"""Commercial admission for new local analytics processing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import RLock
from typing import Literal

from app.persistence.auth import (
    AuthenticationStateError,
    SQLiteAuthenticationStore,
)
from app.security.runtime_policy import (
    AnalysisRunContext,
    AuthContext,
    RuntimeAuthorizationDenied,
    RuntimePolicy,
    require_analysis_run,
)

_ANALYSIS_CAPABILITY = "analysis-run"
_ANALYSIS_ARTIFACT_FAMILY = "analysis-artifact"
_ANALYSIS_MAJOR_VERSION = 3
_ANALYSIS_POLICIES: dict[str, RuntimePolicy] = {}
_ANALYSIS_POLICIES_LOCK = RLock()

CommercialReadiness = Literal["required", "active", "unavailable"]
AnalysisAdmissionReadiness = Literal["blocked", "admitted"]


@dataclass(frozen=True, slots=True)
class AnalysisReadiness:
    """Closed, nonsecret customer-safe view of commercial analysis readiness."""

    commercial_authority: CommercialReadiness
    analysis_admission: AnalysisAdmissionReadiness


def _denied(message: str) -> RuntimeAuthorizationDenied:
    return RuntimeAuthorizationDenied(message)


def _bound_capability_reference_ids(
    store: SQLiteAuthenticationStore,
    policy: RuntimePolicy,
) -> tuple[str, ...]:
    """Return verified local commercial references bound to this runtime.

    Commercial activation is intentionally evaluated separately from whether the
    locally selected analysis major is currently admissible.  This keeps an
    accepted commercial authority visible as active even when analysis is
    independently blocked by version/capability policy.
    """

    identity_authority = policy.identity_authority
    if identity_authority is None:
        raise _denied("Current identity/account authority is required")
    try:
        with store.database.read() as connection:
            rows = connection.execute(
                """
                SELECT reference_id
                FROM capability_license_references
                WHERE organization_id = ?
                  AND installation_id = ?
                  AND installation_key_id = ?
                  AND installation_key_jkt = ?
                  AND capability = ?
                  AND compatible_artifact_family = ?
                ORDER BY reference_id
                """,
                (
                    identity_authority.organization_id,
                    identity_authority.installation_id,
                    identity_authority.installation_key_id,
                    identity_authority.installation_key_jkt,
                    _ANALYSIS_CAPABILITY,
                    _ANALYSIS_ARTIFACT_FAMILY,
                ),
            ).fetchall()
    except Exception as error:
        raise _denied("CapabilityLicense authority is unavailable") from error
    return tuple(str(row["reference_id"]) for row in rows)


def _matching_capability_reference_ids(
    store: SQLiteAuthenticationStore,
    policy: RuntimePolicy,
) -> tuple[str, ...]:
    identity_authority = policy.identity_authority
    if identity_authority is None:
        raise _denied("Current identity/account authority is required")
    try:
        with store.database.read() as connection:
            rows = connection.execute(
                """
                SELECT reference_id, licensed_major_version, fallback_major_versions
                FROM capability_license_references
                WHERE organization_id = ?
                  AND installation_id = ?
                  AND installation_key_id = ?
                  AND installation_key_jkt = ?
                  AND capability = ?
                  AND compatible_artifact_family = ?
                ORDER BY reference_id
                """,
                (
                    identity_authority.organization_id,
                    identity_authority.installation_id,
                    identity_authority.installation_key_id,
                    identity_authority.installation_key_jkt,
                    _ANALYSIS_CAPABILITY,
                    _ANALYSIS_ARTIFACT_FAMILY,
                ),
            ).fetchall()
    except Exception as error:
        raise _denied("CapabilityLicense authority is unavailable") from error

    matches: list[str] = []
    for row in rows:
        try:
            fallback = json.loads(str(row["fallback_major_versions"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise _denied("CapabilityLicense authority is invalid") from error
        if not isinstance(fallback, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in fallback
        ):
            raise _denied("CapabilityLicense authority is invalid")
        licensed_major = row["licensed_major_version"]
        if (
            isinstance(licensed_major, bool)
            or not isinstance(licensed_major, int)
            or licensed_major < 1
        ):
            raise _denied("CapabilityLicense authority is invalid")
        if (
            licensed_major == _ANALYSIS_MAJOR_VERSION
            or _ANALYSIS_MAJOR_VERSION in fallback
        ):
            matches.append(str(row["reference_id"]))
    return tuple(matches)


def _analysis_context(policy: RuntimePolicy) -> AnalysisRunContext:
    identity = policy.identity_authority
    commercial = policy.commercial_authority
    return AnalysisRunContext(
        organization_id=(
            identity.organization_id if identity is not None else "missing"
        ),
        installation_id=(
            identity.installation_id if identity is not None else "missing"
        ),
        installation_key_id=(
            identity.installation_key_id if identity is not None else "missing"
        ),
        installation_key_jkt=(
            identity.installation_key_jkt if identity is not None else "missing"
        ),
        seat_id=commercial.seat_id if commercial is not None else "missing",
        seat_scope=(
            commercial.seat_scope if commercial is not None else "missing"
        ),
        capability=_ANALYSIS_CAPABILITY,
        selected_major_version=_ANALYSIS_MAJOR_VERSION,
        artifact_family=_ANALYSIS_ARTIFACT_FAMILY,
    )


def require_current_analysis_run(policy: RuntimePolicy) -> None:
    """Require current identity/account and CapabilityLicense authority."""

    require_analysis_run(policy, _analysis_context(policy))


def _cache_analysis_policy(policy: RuntimePolicy) -> None:
    identity = policy.identity
    if identity is None:
        raise _denied("Authenticated analysis identity is required")
    with _ANALYSIS_POLICIES_LOCK:
        _ANALYSIS_POLICIES[identity.creator_account_id] = policy


def _current_identity_policy(
    store: SQLiteAuthenticationStore,
    identity: AuthContext,
) -> RuntimePolicy:
    if identity.role != "agent":
        raise _denied("Agent authority is required for analysis scheduling")

    bindings = tuple(
        binding
        for binding in store.authorized_account_bindings()
        if binding.creator_account_id == identity.creator_account_id
        and binding.revoked_at is None
    )
    if len(bindings) != 1:
        raise _denied("Current identity/account authority is required")
    binding = bindings[0]

    try:
        policy = store.build_runtime_policy_from_grants(
            identity,
            tuple(binding.grant_reference_ids),
        )
    except (AuthenticationStateError, ValueError) as error:
        raise _denied("Current identity/account authority is required") from error

    authority = policy.identity_authority
    if authority is None or authority.installation_id != binding.installation_id:
        raise _denied("Current identity/account authority is required")
    return policy


def _current_bound_commercial_policy(
    store: SQLiteAuthenticationStore,
    identity: AuthContext,
    identity_policy: RuntimePolicy,
) -> RuntimePolicy:
    """Compose current commercial authority without deciding analysis admission."""

    reference_ids = _bound_capability_reference_ids(store, identity_policy)
    if not reference_ids:
        raise _denied("CapabilityLicense authority is required")
    if len(reference_ids) != 1:
        raise _denied("CapabilityLicense authority is ambiguous")

    bindings = tuple(
        binding
        for binding in store.authorized_account_bindings()
        if binding.creator_account_id == identity.creator_account_id
        and binding.revoked_at is None
    )
    if len(bindings) != 1:
        raise _denied("Current identity/account authority is required")
    binding = bindings[0]
    try:
        policy = store.build_runtime_policy_with_capability(
            identity,
            tuple(binding.grant_reference_ids),
            reference_ids[0],
        )
    except (AuthenticationStateError, ValueError) as error:
        raise _denied("CapabilityLicense authority is unavailable") from error

    commercial = policy.commercial_authority
    if commercial is None or commercial.reference_id != reference_ids[0]:
        raise _denied("CapabilityLicense authority is unavailable")
    if _bound_capability_reference_ids(store, policy) != reference_ids:
        raise _denied("CapabilityLicense authority changed during composition")
    if not store.runtime_policy_is_current(policy):
        raise _denied("Runtime policy authority changed during composition")
    return policy


def _current_commercial_policy(
    store: SQLiteAuthenticationStore,
    identity: AuthContext,
    identity_policy: RuntimePolicy,
) -> RuntimePolicy:
    reference_ids = _matching_capability_reference_ids(store, identity_policy)
    if not reference_ids:
        raise _denied("CapabilityLicense authority is required")
    if len(reference_ids) != 1:
        raise _denied("CapabilityLicense authority is ambiguous")

    bindings = tuple(
        binding
        for binding in store.authorized_account_bindings()
        if binding.creator_account_id == identity.creator_account_id
        and binding.revoked_at is None
    )
    if len(bindings) != 1:
        raise _denied("Current identity/account authority is required")
    binding = bindings[0]
    try:
        policy = store.build_runtime_policy_with_capability(
            identity,
            tuple(binding.grant_reference_ids),
            reference_ids[0],
        )
    except (AuthenticationStateError, ValueError) as error:
        raise _denied("CapabilityLicense authority is unavailable") from error

    if _matching_capability_reference_ids(store, policy) != reference_ids:
        raise _denied("CapabilityLicense authority changed during composition")
    if not store.runtime_policy_is_current(policy):
        raise _denied("Runtime policy authority changed during composition")
    require_current_analysis_run(policy)
    return policy


def _compose_current_analysis_policy(
    store: SQLiteAuthenticationStore,
    identity: AuthContext,
) -> RuntimePolicy:
    identity_policy = _current_identity_policy(store, identity)
    return _current_commercial_policy(store, identity, identity_policy)


def current_analysis_readiness(
    store: SQLiteAuthenticationStore,
    identity: AuthContext,
) -> AnalysisReadiness:
    """Evaluate commercial activation and analysis admission independently.

    The returned values contain no authority material.  Commercial activation is
    active only after a verified local CapabilityLicense reference can be
    reconstructed for the current account/installation/key.  Licensed-analysis
    admission is then evaluated as a separate predicate for the current Product
    major and artifact family.
    """

    try:
        identity_policy = _current_identity_policy(store, identity)
        _current_bound_commercial_policy(store, identity, identity_policy)
    except RuntimeAuthorizationDenied as error:
        if str(error) == "CapabilityLicense authority is required":
            return AnalysisReadiness("required", "blocked")
        return AnalysisReadiness("unavailable", "blocked")

    try:
        _current_commercial_policy(store, identity, identity_policy)
    except RuntimeAuthorizationDenied:
        return AnalysisReadiness("active", "blocked")
    return AnalysisReadiness("active", "admitted")


def build_current_analysis_policy(
    store: SQLiteAuthenticationStore,
    identity: AuthContext,
) -> RuntimePolicy:
    """Compose and cache current authority for one account's new analysis work.

    The durable account binding supplies the full current grant tuple. Exactly
    one persisted verified CapabilityLicense must match that tuple's
    installation/key plus the locally selected capability, artifact family,
    and major version. Ambiguity fails closed; no recency rule is used.
    """

    policy = _compose_current_analysis_policy(store, identity)
    _cache_analysis_policy(policy)
    return policy


def require_cached_analysis_run(
    store: SQLiteAuthenticationStore,
    creator_account_id: str,
) -> RuntimePolicy:
    """Revalidate the admitted policy immediately before candidate construction."""

    with _ANALYSIS_POLICIES_LOCK:
        policy = _ANALYSIS_POLICIES.get(creator_account_id)
    if policy is None:
        raise _denied("Current analysis admission is required")
    identity = policy.identity
    if identity is None or identity.creator_account_id != creator_account_id:
        clear_analysis_policy(creator_account_id)
        raise _denied("Current analysis admission does not match the account")
    if not store.runtime_policy_is_current(policy):
        clear_analysis_policy(creator_account_id)
        raise _denied("Current analysis admission is stale")
    try:
        require_current_analysis_run(policy)
    except RuntimeAuthorizationDenied:
        clear_analysis_policy(creator_account_id)
        raise
    return policy


def clear_analysis_policy(creator_account_id: str) -> None:
    with _ANALYSIS_POLICIES_LOCK:
        _ANALYSIS_POLICIES.pop(creator_account_id, None)


def clear_analysis_policies() -> None:
    with _ANALYSIS_POLICIES_LOCK:
        _ANALYSIS_POLICIES.clear()
