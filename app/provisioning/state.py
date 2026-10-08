"""Brain-owned onboarding facts, evaluated from current local authority."""
from __future__ import annotations

import base64

from app.persistence.auth import SQLiteAuthenticationStore, AuthenticationStateError, RevocationKey, RevocationScopeType
from app.persistence.companion_pairing import CompanionPairingPersistence, CompanionPairingPersistenceError
from app.persistence.onboarding import OnboardingJourneyStore
from app.provisioning.events import events
from app.security.analysis_authorization import current_analysis_readiness
from app.security.runtime_policy import AuthContext
from app.security.webauthn import WebAuthnAuthorityPort, WebAuthnAuthorityResult


def next_authority_expiry(store: SQLiteAuthenticationStore) -> float | None:
    """One timer for known validity boundaries; no repeating status polling."""
    now = store._now()
    boundaries = [value for grant in store.verified_grants()
                  for value in (grant.valid_from, grant.expires_at) if value > now]
    return None if not boundaries else (min(boundaries) - now).total_seconds()


def brain_snapshot(store: SQLiteAuthenticationStore, journey_id: str, identity: AuthContext | None = None) -> dict:
    journey = OnboardingJourneyStore(store).get(journey_id)
    if journey is None:
        raise ValueError("Journey is unavailable")
    facts = {name: "unknown" for name in ("installation", "enrollment", "pairing", "activation", "analysis")}
    policy = store.build_runtime_policy(identity)
    authority = WebAuthnAuthorityPort(store, clock=store._now).session_authority(policy)
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
    elif not installation and journey["state"] in {"new", "preparing", "waiting"}:
        facts["installation"] = "missing"
    if identity is not None:
        readiness = current_analysis_readiness(store, identity)
        facts["activation"] = {"active": "verified", "required": "missing", "unavailable": "unknown"}[readiness.commercial_authority]
        facts["analysis"] = "verified" if readiness.analysis_admission == "admitted" else "unknown"
        with store.database.transaction() as connection:
            rows = connection.execute("SELECT pairing_id, pairing_generation FROM agent_pairings WHERE creator_account_id = ? AND revoked_at IS NULL",
                                      (identity.creator_account_id,)).fetchall()
            facts["pairing"] = "missing" if not rows else "unknown"
            for row in rows:
                if row["pairing_generation"] is not None:
                    continue
                try:
                    store._require_pairing_current(connection, row["pairing_id"], now)
                except AuthenticationStateError:
                    continue
                facts["pairing"] = "verified"
        for row in rows:
            if row["pairing_generation"] is None:
                continue
            try:
                CompanionPairingPersistence(store).session_authority(base64.urlsafe_b64decode(row["pairing_id"] + "="))
            except (CompanionPairingPersistenceError, AuthenticationStateError, ValueError):
                continue
            facts["pairing"] = "verified"
    reason = {"revoked": "authorization_revoked", "expired": "authorization_expired",
              "unknown": "operation_unconfirmed", "prepare-unknown": "operation_unconfirmed"}.get(journey["state"], "none")
    # The published local command ID is v4, while hosted operations are v7. The
    # hosted ID stays in its own durable journal and is never mislabelled locally.
    return events.snapshot(journey_id=journey_id, facts=facts, reason=reason)
