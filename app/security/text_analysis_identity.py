"""Concrete identity snapshots for retained licensed enrollment scope."""
from app.models.text_analysis_enrollment import EnrollmentIdentity
from app.nlp.profiles import canonical_bytes
from app.persistence.auth import (
    AuthenticationStateError, RevocationKey, RevocationScopeType, SQLiteAuthenticationStore,
)
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.runtime_policy import (
    AuthContext, IdentityAccountAuthority, RuntimeAuthorizationDenied, RuntimePolicy,
)


def _deny():
    raise RuntimeAuthorizationDenied('text_analysis_identity_unavailable')


def _scope(policy: RuntimePolicy) -> EnrollmentIdentity:
    identity, authority = policy.identity, policy.identity_authority
    if (type(identity) is not AuthContext or identity.role not in ('creator', 'operator')
            or type(authority) is not IdentityAccountAuthority
            or not set(ACCOUNT_AUTHORITY_GRANT_TYPES).issubset(authority.grant_types)):
        _deny()
    return EnrollmentIdentity(principal_id=identity.principal_id, creator_account_id=identity.creator_account_id,
        role=identity.role, organization_id=authority.organization_id, installation_id=authority.installation_id,
        installation_key_id=authority.installation_key_id, installation_key_jkt=authority.installation_key_jkt,
        grant_reference_ids=authority.grant_reference_ids)


class TextAnalysisIdentityOwner:
    """Rebuild original verified references without retaining a browser session.

    Background input comes from the integrity-checked canonical capsule. Its
    original local principal and role are not independently supplied by grants.
    Execution transitions still require their separate current authority fence.
    """

    def __init__(self, authentication: SQLiteAuthenticationStore):
        if type(authentication) is not SQLiteAuthenticationStore:
            raise ValueError('text_analysis_authentication_owner_required')
        self.authentication = authentication

    def _not_revoked(self, identity: EnrollmentIdentity) -> None:
        scopes = ((RevocationScopeType.PRINCIPAL, identity.principal_id),
                  (RevocationScopeType.CREATOR_ACCOUNT, identity.creator_account_id),
                  (RevocationScopeType.INSTALLATION, identity.installation_id))
        if any(self.authentication.scope_is_revoked(RevocationKey(kind, value)) for kind, value in scopes):
            _deny()

    def capture(self, policy: RuntimePolicy) -> EnrollmentIdentity:
        """Capture one actual current durable Bridge session's identity subset."""
        if type(policy) is not RuntimePolicy or type(policy.identity) is not AuthContext:
            _deny()
        auth = self.authentication
        with auth.database.dependency_authority_gate:
            if not auth.bridge_session_is_current(policy):
                _deny()
            with auth.database.read() as db:
                references = auth._session_grants(db, policy.identity.session_id)
            supplied = policy.signed_object_reference_ids
            if len(supplied) != len(set(supplied)) or set(references) != set(supplied):
                _deny()
            try:
                rebuilt = auth.build_runtime_policy_from_grants(policy.identity, supplied)
                original = _scope(rebuilt)
                if policy.identity_authority is not None and _scope(policy) != original:
                    _deny()
            except (AuthenticationStateError, ValueError):
                _deny()
            self._not_revoked(original)
            if not auth.runtime_policy_is_current(policy) or not auth.runtime_policy_is_current(rebuilt):
                _deny()
            return original

    def rebuild(self, original: EnrollmentIdentity) -> RuntimePolicy:
        """Read baseline identity from exact retained lineage, without refreshing it."""
        if type(original) is not EnrollmentIdentity:
            _deny()
        try:
            original = EnrollmentIdentity.model_validate_json(canonical_bytes(original))
        except ValueError:
            _deny()
        auth = self.authentication
        with auth.database.dependency_authority_gate:
            self._not_revoked(original)
            identity = AuthContext(original.principal_id, original.creator_account_id, original.role)
            try:
                policy = auth.build_runtime_policy_from_grants(identity, original.grant_reference_ids)
                if _scope(policy) != original:
                    _deny()
            except (AuthenticationStateError, ValueError):
                _deny()
            self._not_revoked(original)
            if not auth.runtime_policy_is_current(policy):
                _deny()
            return policy
