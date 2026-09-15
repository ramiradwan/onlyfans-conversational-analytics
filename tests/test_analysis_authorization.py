from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.persistence.auth import AuthorizedAccountBinding
from app.security.analysis_authorization import (
    build_current_analysis_policy,
    require_current_analysis_run,
)
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.runtime_policy import (
    AuthContext,
    AuthorizationEpoch,
    CapabilityLicenseAuthority,
    IdentityAccountAuthority,
    RuntimeAuthorizationDenied,
    RuntimePolicy,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
ORGANIZATION_ID = "org.acme"
INSTALLATION_ID = "install.primary"
INSTALLATION_KEY_ID = "ik1.CCCCCCCCCCCCCCCCCCCCCC"
INSTALLATION_KEY_JKT = "A" * 43
IDENTITY = AuthContext("principal-1", "creator-1", "agent")
GRANTS = ("installation", "membership", "binding")


def _identity_authority() -> IdentityAccountAuthority:
    return IdentityAccountAuthority(
        organization_id=ORGANIZATION_ID,
        installation_id=INSTALLATION_ID,
        installation_key_id=INSTALLATION_KEY_ID,
        installation_key_jkt=INSTALLATION_KEY_JKT,
        grant_reference_ids=GRANTS,
        grant_types=tuple(ACCOUNT_AUTHORITY_GRANT_TYPES),
    )


def _commercial(
    *,
    reference_id: str = "caplic.ref",
    licensed_major_version: int = 3,
    fallback_major_versions: tuple[int, ...] = (),
) -> CapabilityLicenseAuthority:
    return CapabilityLicenseAuthority(
        reference_id=reference_id,
        object_digest="d" * 64,
        license_id="license-001",
        issuance_id="issuance-001",
        subject=(
            "organization:org.acme:installation:install.primary:"
            "seat:seat.analysis.001:capability:analysis-run"
        ),
        organization_id=ORGANIZATION_ID,
        installation_id=INSTALLATION_ID,
        installation_key_id=INSTALLATION_KEY_ID,
        installation_key_jkt=INSTALLATION_KEY_JKT,
        seat_id="seat.analysis.001",
        seat_scope="organization-installation-seat",
        capability="analysis-run",
        licensed_major_version=licensed_major_version,
        compatible_artifact_family="analysis-artifact",
        update_rights=False,
        fallback_major_versions=fallback_major_versions,
        signer_kid="bc1.cl.test",
    )


def _policy(
    *,
    identity_authority: bool = True,
    commercial: CapabilityLicenseAuthority | None = None,
    include_commercial: bool = True,
) -> RuntimePolicy:
    return RuntimePolicy(
        identity=IDENTITY,
        authorization_epoch=AuthorizationEpoch(1),
        identity_authority=_identity_authority() if identity_authority else None,
        commercial_authority=(
            (commercial or _commercial()) if include_commercial else None
        ),
    )


def _binding(*, revoked: bool = False) -> AuthorizedAccountBinding:
    return AuthorizedAccountBinding(
        creator_account_id=IDENTITY.creator_account_id,
        installation_id=INSTALLATION_ID,
        platform_creator_id="platform.creator.1",
        association_request_id="association-1",
        grant_bundle_sha256="a" * 64,
        authorized_at=NOW,
        grant_reference_ids=GRANTS,
        revoked_at=NOW if revoked else None,
    )


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _Result:
        assert "FROM capability_license_references" in statement
        assert parameters == (
            ORGANIZATION_ID,
            INSTALLATION_ID,
            INSTALLATION_KEY_ID,
            INSTALLATION_KEY_JKT,
            "analysis-run",
            "analysis-artifact",
        )
        return _Result(self._rows)


class _Database:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    @contextmanager
    def read(self):
        yield _Connection(self._rows)


class _Store:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        binding: AuthorizedAccountBinding | None = None,
    ) -> None:
        self.database = _Database(rows)
        self.binding = binding or _binding()
        self.reference_ids: list[str] = []

    def authorized_account_bindings(self) -> tuple[AuthorizedAccountBinding, ...]:
        return (self.binding,)

    def build_runtime_policy_from_grants(
        self,
        identity: AuthContext,
        grant_reference_ids: tuple[str, ...],
    ) -> RuntimePolicy:
        assert identity == IDENTITY
        assert grant_reference_ids == GRANTS
        return RuntimePolicy(
            identity=identity,
            authorization_epoch=AuthorizationEpoch(1),
            identity_authority=_identity_authority(),
        )

    def build_runtime_policy_with_capability(
        self,
        identity: AuthContext,
        grant_reference_ids: tuple[str, ...],
        capability_reference_id: str,
    ) -> RuntimePolicy:
        self.reference_ids.append(capability_reference_id)
        return replace(
            self.build_runtime_policy_from_grants(identity, grant_reference_ids),
            commercial_authority=_commercial(reference_id=capability_reference_id),
        )

    def runtime_policy_is_current(self, policy: RuntimePolicy) -> bool:
        return True


def _row(
    reference_id: str,
    *,
    licensed_major_version: int = 3,
    fallback_major_versions: str = "[]",
) -> dict[str, object]:
    return {
        "reference_id": reference_id,
        "licensed_major_version": licensed_major_version,
        "fallback_major_versions": fallback_major_versions,
    }


def test_new_analysis_denied_without_capability_license() -> None:
    with pytest.raises(RuntimeAuthorizationDenied):
        require_current_analysis_run(_policy(include_commercial=False))


def test_new_analysis_denied_without_identity_account_authority() -> None:
    with pytest.raises(RuntimeAuthorizationDenied):
        require_current_analysis_run(_policy(identity_authority=False))


def test_new_analysis_denied_when_license_does_not_cover_local_major() -> None:
    with pytest.raises(RuntimeAuthorizationDenied):
        require_current_analysis_run(
            _policy(commercial=_commercial(licensed_major_version=4))
        )


def test_new_analysis_allowed_by_explicit_fallback_for_local_major() -> None:
    require_current_analysis_run(
        _policy(
            commercial=_commercial(
                licensed_major_version=4,
                fallback_major_versions=(3,),
            )
        )
    )


def test_new_analysis_allowed_with_both_current_authorities() -> None:
    require_current_analysis_run(_policy())


def test_policy_composition_uses_unique_compatible_license() -> None:
    store = _Store([_row("caplic.unique")])

    policy = build_current_analysis_policy(store, IDENTITY)  # type: ignore[arg-type]

    assert store.reference_ids == ["caplic.unique"]
    require_current_analysis_run(policy)


def test_policy_composition_accepts_explicit_fallback_for_local_major() -> None:
    store = _Store(
        [_row("caplic.fallback", licensed_major_version=4, fallback_major_versions="[3]")]
    )

    policy = build_current_analysis_policy(store, IDENTITY)  # type: ignore[arg-type]

    assert policy.commercial_authority is not None


def test_policy_composition_fails_closed_without_compatible_license() -> None:
    store = _Store([_row("caplic.v4", licensed_major_version=4)])

    with pytest.raises(RuntimeAuthorizationDenied, match="CapabilityLicense"):
        build_current_analysis_policy(store, IDENTITY)  # type: ignore[arg-type]


def test_policy_composition_fails_closed_on_ambiguous_licenses() -> None:
    store = _Store([_row("caplic.one"), _row("caplic.two")])

    with pytest.raises(RuntimeAuthorizationDenied, match="ambiguous"):
        build_current_analysis_policy(store, IDENTITY)  # type: ignore[arg-type]


def test_policy_composition_fails_closed_for_revoked_account_binding() -> None:
    store = _Store([_row("caplic.unique")], binding=_binding(revoked=True))

    with pytest.raises(RuntimeAuthorizationDenied, match="identity/account"):
        build_current_analysis_policy(store, IDENTITY)  # type: ignore[arg-type]
