<!-- CODE-VERIFY: app/security/activation_delivery_recovery.py, app/security/capability_license_redemption_journal.py, app/security/capability_license_delivery_journal.py, app/security/capability_license_composition.py, app/onboarding/activation.py, app/persistence/auth.py, app/persistence/auth_sql/0031_activation_delivery_recovery.sql, tests/test_activation_delivery_recovery.py, tests/test_capability_license_delivery_recovery.py, tests/test_capability_license_shipping.py. -->

# ADR 0052: Recover delivery of an accepted activation

- Status: Accepted.
- Date: 2026-10-13.
- Decision authority: Product owner's approval of automatic recovery for the same accepted activation, installation key and creator.
- Extends: [ADR 0025](0025-adopt-capability-license-delivery-contracts.md) and [ADR 0051](0051-native-account-and-activation-decisions.md).

## Decision

Brain adopts the published [delivery recovery](../../contracts/profiles/capability-license-delivery-recovery/v1/profile.json) and [separate proof](../../contracts/profiles/capability-license-delivery-recovery-proof/v1/profile.json) contracts from the verified [consumer pin](../../contracts/consumer-pin.json). Provider remains the authority for the original activation, credit debit, generation and delivery authorization. Recovery cannot purchase another capability or change the accepted scope.

Brain records the original activation identifier, Provider origin, installation, organization, creator and installation key before authentication or commercial submission. Its encrypted authentication database retains delivery state independently of browser and desktop presentation expiry. Missing or mismatched original records fail closed; no legacy reconstruction is added.

The delivery owner replays any pending opaque redemption or license issuance before requesting recovery. It saves exact requests and idempotency keys before sending them, and saves the protected redemption response before issuing the license. A restart reuses those records without requiring a new proof challenge for an already submitted request.

An exact redemption-commit response may establish terminal non-delivery only through the closed common error contract: HTTP 410 with `expired`/`continuation_expired`, or HTTP 404 with `not_found`/`continuation_invalid`. Brain retains that immutable refusal with the original request and replays it locally. Such a row no longer blocks workspace reconciliation; uncertain requests, protected results, committed activation work and pending issuance remain independently protected.

An expired unredeemed authorization may be recovered automatically through the original activation. Brain persists the recovery generation and exact proof envelope before sending. An unknown result is replayed or read for that same generation; a `not-issued` result requires current-generation reconciliation before retry. Brain keeps a monotonic generation and durable retry deadline. A read or an expired browser presentation cannot create a replacement commercial operation.

Protected packages, continuations and proof envelopes remain inside Brain's encrypted database. The renderer and extension receive no delivery authority. License verification still uses the existing signature, installation, key, capability and version checks. First persistence and exact verified replay atomically settle only journals linked to that signed object. The coordinator advances from verified local readiness, never from browser closure or a Provider delivery receipt alone.

A completed opaque redemption can return its local receipt after re-verifying the associated saved license against the current installation and key. Its Provider origin, protected package, seat and delivery reference must still match. Missing or inconsistent completion evidence fails closed. An unresolved issuance retains priority; a completion that wins before request preparation prevents a new issuance request.

Recovery adds no runtime heartbeat. A verified capability retains the existing local and offline behavior. Account connection, commercial acceptance, collection consent and capability verification remain separate authorities.

## Verification

Consume the published parser, schema and proof vectors independently. Exercise expiry, ambiguous sends before and after commit, exact replay after restart, scope changes, lost journals, retry deadlines and atomic license persistence. Qualify the matching installed Product and Provider with an expired accepted delivery, then restart offline and verify the saved capability.
