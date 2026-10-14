"""Brain-owned onboarding facts, evaluated from current local authority."""
from __future__ import annotations

from app.persistence.auth import SQLiteAuthenticationStore, RevocationKey, RevocationScopeType
from app.persistence.companion_pairing import CompanionPairingPersistence
from app.persistence.onboarding import OnboardingJourneyStore
from app.provisioning.events import events
from app.security.analysis_authorization import AnalysisReadiness, current_analysis_readiness
from app.security.runtime_policy import AuthContext
from app.security.webauthn import WebAuthnAuthorityPort, WebAuthnAuthorityResult


def next_authority_expiry(store: SQLiteAuthenticationStore) -> float | None:
    """One timer for known validity boundaries; no repeating status polling."""
    now = store._now()
    boundaries = [value for grant in store.verified_grants()
                  for value in (grant.valid_from, grant.expires_at) if value > now]
    return None if not boundaries else (min(boundaries) - now).total_seconds()


def brain_snapshot(store: SQLiteAuthenticationStore, journey_id: str, identity: AuthContext | None = None) -> dict:
    journey = OnboardingJourneyStore(store).presentation(journey_id)
    if journey is None:
        raise ValueError("Journey is unavailable")
    facts = local_setup_facts(store, journey=journey, identity=identity)
    reason = {"revoked": "authorization_revoked", "expired": "authorization_expired",
              "unknown": "operation_unconfirmed", "prepare-unknown": "operation_unconfirmed"}.get(journey["state"], "none")
    # Hosted operation identifiers stay in their own journals.
    return events.snapshot(journey_id=journey_id, facts=facts, reason=reason)


def local_setup_facts(store: SQLiteAuthenticationStore, *, journey: dict | None = None,
                      identity: AuthContext | None = None, connection=None,
                      readiness: AnalysisReadiness | None = None) -> dict:
    """Project the existing local authority owners without coordinating work."""
    facts = {name: "unknown" for name in ("installation", "enrollment", "pairing", "activation", "analysis")}
    policy = store.build_runtime_policy(identity)
    authority = WebAuthnAuthorityPort(store, clock=store._now).session_authority(policy, connection=connection)
    if authority.result is WebAuthnAuthorityResult.AUTHORIZED:
        facts["enrollment"] = "verified"
    elif authority.result is WebAuthnAuthorityResult.CREDENTIAL_MISSING:
        facts["enrollment"] = "missing"
    grants = store.verified_grants()
    now = store._now()
    installation = [grant for grant in grants if grant.grant_type == "installation_grant" and grant.valid_from <= now < grant.expires_at]
    key = store.installation_key_reference()
    if (key is not None and len(installation) == 1
            and installation[0].installation_key_jkt == key.installation_key_jkt
            and not store.scope_is_revoked(RevocationKey(RevocationScopeType.INSTALLATION, installation[0].installation_id))
            and not store.scope_is_revoked(RevocationKey(RevocationScopeType.VERIFIED_GRANT, installation[0].reference_id))):
        facts["installation"] = "verified"
    elif journey is None and not installation:
        facts["installation"] = "missing"
    elif (journey is not None and journey["kind"] == "initial-enrollment" and not installation
          and journey["state"] in {"new", "preparing", "waiting"}):
        facts["installation"] = "missing"
    if identity is not None:
        # The readiness owner evaluates the analysis agent's account. This is a
        # read-only projection, not agent authentication or cached run admission.
        if readiness is None:
            readiness = current_analysis_readiness(store, AuthContext(
                identity.principal_id, identity.creator_account_id, "agent"))
        facts["activation"] = {"active": "verified", "required": "missing", "unavailable": "unknown"}[readiness.commercial_authority]
        facts["analysis"] = "verified" if readiness.analysis_admission == "admitted" else "unknown"
        facts["pairing"] = CompanionPairingPersistence(store).current_pairing_state(
            identity.creator_account_id, connection=connection
        )
    return facts
