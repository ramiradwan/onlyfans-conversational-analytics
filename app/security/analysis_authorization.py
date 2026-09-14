"""Commercial admission for new local analytics processing."""

from __future__ import annotations

import json

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


def _denied(message: str) -> RuntimeAuthorizationDenied:
    return RuntimeAuthorizationDenied(message)


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


def build_current_analysis_policy(
    store: SQLiteAuthenticationStore,
    identity: AuthContext,
) -> RuntimePolicy:
    """Compose current identity/account and commercial authority for one run.

    The durable account binding supplies the full current grant tuple. The
    CapabilityLicense is selected only when exactly one persisted verified
    object matches that tuple's installation/key and the locally selected
    analysis capability, artifact family, and major version. No recency or
    insertion order is used to resolve ambiguity.
    """

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
        identity_policy = store.build_runtime_policy_from_grants(
            identity,
            tuple(binding.grant_reference_ids),
        )
    except (AuthenticationStateError, ValueError) as error:
        raise _denied("Current identity/account authority is required") from error

    identity_authority = identity_policy.identity_authority
    if (
        identity_authority is None
        or identity_authority.installation_id != binding.installation_id
    ):
        raise _denied("Current identity/account authority is required")

    reference_ids = _matching_capability_reference_ids(store, identity_policy)
    if not reference_ids:
        raise _denied("CapabilityLicense authority is required")
    if len(reference_ids) != 1:
        raise _denied("CapabilityLicense authority is ambiguous")

    try:
        policy = store.build_runtime_policy_with_capability(
            identity,
            tuple(binding.grant_reference_ids),
            reference_ids[0],
        )
    except (AuthenticationStateError, ValueError) as error:
        raise _denied("CapabilityLicense authority is required") from error

    # Recheck both uniqueness and the composed epoch after composition. This
    # closes the normal race where authority changes between selection and use;
    # the decision still fails closed if another matching license appears.
    if _matching_capability_reference_ids(store, policy) != reference_ids:
        raise _denied("CapabilityLicense authority changed during composition")
    if not store.runtime_policy_is_current(policy):
        raise _denied("Runtime policy authority changed during composition")
    return policy


def require_current_analysis_run(policy: RuntimePolicy) -> None:
    """Require current identity/account and CapabilityLicense authority.

    Installation coordinates come from the independently verified identity
    plane. The selected capability, artifact family, and major are local runtime
    inputs. The uniquely selected commercial authority contributes the licensed
    seat that is then bound to those local inputs by ``require_analysis_run``.
    """

    identity = policy.identity_authority
    commercial = policy.commercial_authority
    context = AnalysisRunContext(
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
    require_analysis_run(policy, context)
