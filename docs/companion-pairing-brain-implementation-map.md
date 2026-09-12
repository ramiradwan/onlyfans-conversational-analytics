# Brain companion-pairing implementation map

<!-- CODE-VERIFY: Check app/persistence/auth.py, app/persistence/companion_pairing.py, auth_sql/0010_companion_grant_retention.sql, auth_sql/0011_companion_pairing_persistence.sql, auth_sql/0012_companion_pin_admission.sql, app/security/companion_pairing.py, app/security/companion_pairing_proof.py, app/api/endpoints/companion_pairing.py, frontend/src/components/CompanionPairingControls.tsx, and the vendored companion profile. -->

This note maps ADR 0024 and the vendored `urn:bridge-clean:companion-pairing:v1` profile onto Brain security and persistence owners.

## Existing security/storage owners

- **Installation key:** `app/security/installation_key.py`. `InstallationKeyAuthority` coordinates the durable `InstallationKeyReference` with `InstallationKeyProvider`; production uses `WindowsCNGInstallationKeyProvider`. Signing remains non-exportable and hardware-backed.
- **Hosted grant verification and refresh:** `app/security/hosted_grants.py` owns acquisition and refresh; `app/security/grant_verifier.py` owns compact-JWS verification; `app/persistence/auth.py` owns verified-reference currentness and revocation state.
- **`auth.sqlite3` transactions:** `app/persistence/database.py` (`AuthSQLite` / `LocalSQLite.transaction`) uses `BEGIN IMMEDIATE` by default and rolls back on any exception.
- **Protected local secrets:** `app/security/local_data_key.py` owns wrapping and unwrapping. Persistence treats protected Brain Noise private-key bytes as opaque.
- **Bridge authentication/session state:** `app/api/security.py`, `app/security/webauthn.py`, and `app/api/endpoints/webauthn.py` own authenticated Bridge identity and session checks.
- **Admitted Agent pairing authority:** `AgentPairing` and `agent_pairings` in `app/persistence/auth.py` remain the only durable authority used by Agent challenges, tickets, `_require_pairing_current()`, and runtime authorization.
- **WebSocket and active-session lifecycle:** `app/api/endpoints/companion_pairing.py` adapts the pairing exchange. `app/api/endpoints/transport_ws.py` and `app/transport/manager.py` own runtime routing and active socket lifecycle. Pairing staging does not authorize runtime traffic.

## Companion pairing persistence ownership

Pre-admission pairing state is separate staging data. It does not create a second admitted-pairing authority.

| State/capability | Owner |
| --- | --- |
| pairing generation high-water | `companion_pairing_generations` in `app/persistence/auth_sql/0011_companion_pairing_persistence.sql` |
| pre-admission window, state and CAS version | `companion_pairing_windows` in the same migration; typed access in `app/persistence/companion_pairing.py` |
| wrapped candidate Brain Noise private key | opaque BLOB in `companion_pairing_windows`; wrapping remains in `app/security/local_data_key.py` |
| exact frozen pairing grant references | foreign keys from `companion_pairing_windows` to `verified_grant_references` |
| confirming Bridge principal/session | staging fields in `companion_pairing_windows` |
| confirmed durable Agent pin | existing `agent_pairings`, using existing `public_key` / `key_fingerprint` for Agent P-256 identity and companion fields added by 0011 and 0012 |

`companion_pairing_windows` is never consulted by `_require_pairing_current()`, Agent challenge issuance, Agent ticket issuance, or runtime policy construction. A confirmed staging row is not an admitted Agent pairing.

Generation allocation and every window transition run under the existing `BEGIN IMMEDIATE` transaction path. Time is sampled after acquiring the transaction, so waiting for the database lock cannot extend a deadline. Windows last at most 300 seconds. Generations stop at the wire schema's maximum of `9007199254740991`.

Window mutations use `pairing_id`, expected version, and expected state as a CAS and advance the version exactly once. Cancellation accepts an older displayed version and terminates the current attempt; approval requires the exact displayed version. The schema prevents generation rollback, invalid state transitions, mutation of frozen offer data, and more than one live window per installation.

The four failure states (`declined`, `cancelled`, `expired`, `revoked`) require the protected candidate Brain Noise private key to be cleared in the same row update. The protected key is excluded from record representations and comparison diagnostics. Confirmation revalidates the Bridge session, principal, role, and creator account inside the transaction. A confirmed candidate can still be cancelled or revoked, which clears its key and confirmation fields. Revoking a frozen grant, installation, account, or confirming principal/session also clears the affected candidates atomically.

`open_authorized_window()` requires a current Bridge session and approved account, and freezes the two grants when opening the window. Migration 0012 records the opening authority and first request claim. A second request cancels the window even if the first is still signing its offer.

`confirm_and_admit()` rechecks the session, account, grant references, deadline, and version. One transaction revokes any previous pin for the same Agent installation and account, writes the new pin, and consumes the staging row. The generation high-water mark survives cancellation and revocation. The new pin remains ineligible for the existing plaintext Agent activation and challenge APIs.

`app/security/companion_pairing.py` generates and protects candidate Noise keys, constructs and signs offers, verifies Agent proofs, and delegates state changes to persistence. The pairing WebSocket adapter enforces origin, frame bounds, message deadlines, and cancellation, including late signing completion. Bridge uses `/api/v1/companion/pairings` to open and inspect a window and the versioned `confirm`, `decline`, and `cancel` actions. Its settings panel requires explicit code and account comparison; approval is not reported as an encrypted connection.

## Transport qualification

`app/security/companion_pairing_proof.py` owns the fixed binary pairing transcript, grant digest, role-separated ES256 proofs, comparison code, and Noise prologue. It binds the exact retained grants to their shared identity metadata and the active installation-key reference, and validates the public keys, nonces, and generation bounds. Brain signing uses `InstallationKeyAuthority.sign_challenge()`; the provider's result is verified against the referenced public key before it is returned. Agent proof verification requires canonical unpadded base64url and low-S P1363 signatures.

`tests/test_companion_pairing_proof.py` verifies the vendored snapshot before reading its vectors and exercises the production proof module through the existing installation-key authority with a test provider. Grant currentness, revocation, account approval, and the commit fence remain the persistence and orchestration callers' responsibility.

The native responder is built from `native/companion-snow`; the packaged Agent WASM is built from `extension/crypto/snow`. `app/security/companion_noise.py` constructs the native responder from an admitted pin and a purpose-bound unwrapped key. `app/api/endpoints/companion_session.py` admits the encrypted stream and routes session RPCs. Existing protocol v2 messages enter the transport manager through an adapter whose writes recheck the current session authority.

`app/security/companion_session_authority.py` binds fresh challenges and tickets to one Noise session. Migration 0013 records that binding in authentication rows. Each operation rechecks the pinned installation, creator account, grants, generation and revocation state. Canonical writes hold an authentication transaction across their synchronous commit, so revocation and data admission are serialized. Session closure invalidates outstanding credentials; every reconnect performs a new handshake and identity proof.

Bridge lists current account-scoped pairings through `GET /api/v1/companion/pins`. The versioned `revoke` action clears the protected key and advances revocation state. Connected sessions check that state on traffic and every 250 milliseconds while idle.

Packaged Uvicorn limits incoming WebSocket messages to 36,864 bytes and queues to eight messages, with compression disabled. The encrypted adapter imposes the smaller routine-record limit before decrypting and reconstructs bounded protocol documents. Bridge outgoing snapshots are unaffected by the incoming message limit.

Agent sockets use `127.0.0.1:17871`; Bridge remains on `bridge.localhost:17871`. The Agent origin does not serve HTTP or issue cookies, and the extension has no local host permission. `extension/qualification/companion-origin-browser.mjs` exercises the packaged WASM and production socket modules against a hostile listener with cookies seeded on both origins. The release matrix runs this check on Chrome 132 and current Chromium. The pairing and session adapters independently reject cookies, authorization headers, query parameters and subprotocol credentials.

See the [session transport contract](companion-session-transport.md) for framing, deadlines and encrypted operations.

## Record sizes

Sizes come from `contracts/companion-pairing-profile/profile.json`. Routine application records use a 4,096-byte frame. `session.authorization` uses the 36,864-byte ceiling. Pairing nonces, Noise public keys, pairing digests, and grant digests are 32-byte values and are stored as bounded BLOBs.

## Grant retention and frozen bindings

`VerifiedGrantReference` retains the exact verified compact JWS only for the two companion pairing grant types. `app/persistence/auth_sql/0010_companion_grant_retention.sql` stores those retained bytes alongside the existing digest and metadata.

A window may enter `offered` only with the exact retained references for:

- `installation_grant`
- `creator_account_binding`

The window stores reference IDs and the transcript grant digest, not copied compact JWS strings. The grants must agree on issuer, subject, organization, installation and installation key, and match the active local installation key and requested account. Later staging transitions recheck those exact references for retention and currentness in the same transaction. Refresh or revocation of a frozen reference therefore blocks later progression; refreshed grants are not substituted into an already-frozen transcript.

Companion pairing requires retained grant bytes. A development database without them requires freshly verified grants before it can pair.
