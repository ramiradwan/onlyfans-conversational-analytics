# Brain companion-pairing implementation map

<!-- CODE-VERIFY: Check app/persistence/auth.py, app/persistence/companion_pairing.py, auth_sql/0010_companion_grant_retention.sql, auth_sql/0011_companion_pairing_persistence.sql, app/security/hosted_grants.py, grant_types.py, and the vendored companion profile. -->

This note maps ADR 0024 and the vendored `urn:bridge-clean:companion-pairing:v1` profile onto Brain security and persistence owners.

## Existing security/storage owners

- **Installation key:** `app/security/installation_key.py`. `InstallationKeyAuthority` coordinates the durable `InstallationKeyReference` with `InstallationKeyProvider`; production uses `WindowsCNGInstallationKeyProvider`. Signing remains non-exportable and hardware-backed.
- **Hosted grant verification and refresh:** `app/security/hosted_grants.py` owns acquisition and refresh; `app/security/grant_verifier.py` owns compact-JWS verification; `app/persistence/auth.py` owns verified-reference currentness and revocation state.
- **`auth.sqlite3` transactions:** `app/persistence/database.py` (`AuthSQLite` / `LocalSQLite.transaction`) uses `BEGIN IMMEDIATE` by default and rolls back on any exception.
- **Protected local secrets:** `app/security/local_data_key.py` owns wrapping and unwrapping. Persistence treats protected Brain Noise private-key bytes as opaque.
- **Bridge authentication/session state:** `app/api/security.py`, `app/security/webauthn.py`, and `app/api/endpoints/webauthn.py` own authenticated Bridge identity and session checks.
- **Admitted Agent pairing authority:** `AgentPairing` and `agent_pairings` in `app/persistence/auth.py` remain the only durable authority used by Agent challenges, tickets, `_require_pairing_current()`, and runtime authorization.
- **WebSocket and active-session lifecycle:** `app/api/endpoints/transport_ws.py` and `app/transport/manager.py` own routing and active socket lifecycle. Pairing staging does not authorize a socket.

## Companion pairing persistence ownership

Pre-admission pairing state is separate staging data. It does not create a second admitted-pairing authority.

| State/capability | Owner |
| --- | --- |
| pairing generation high-water | `companion_pairing_generations` in `app/persistence/auth_sql/0011_companion_pairing_persistence.sql` |
| pre-admission window, state and CAS version | `companion_pairing_windows` in the same migration; typed access in `app/persistence/companion_pairing.py` |
| wrapped candidate Brain Noise private key | opaque BLOB in `companion_pairing_windows`; wrapping remains in `app/security/local_data_key.py` |
| exact frozen pairing grant references | foreign keys from `companion_pairing_windows` to `verified_grant_references` |
| confirming Bridge principal/session | staging fields in `companion_pairing_windows` |
| confirmed durable Agent pin | existing `agent_pairings`, using existing `public_key` / `key_fingerprint` for Agent P-256 identity plus nullable companion pin fields added by 0011 |

`companion_pairing_windows` is never consulted by `_require_pairing_current()`, Agent challenge issuance, Agent ticket issuance, or runtime policy construction. A confirmed staging row is not an admitted Agent pairing.

Generation allocation and every window transition run under the existing `BEGIN IMMEDIATE` transaction path. Time is sampled after acquiring the transaction, so waiting for the database lock cannot extend a deadline. Windows last at most 300 seconds. Generations stop at the wire schema's maximum of `9007199254740991`.

Window mutations use `pairing_id`, expected version, and expected state as a CAS and advance the version exactly once. The schema prevents generation rollback, invalid state transitions, mutation of frozen offer data, and more than one live window per installation.

The four failure states (`declined`, `cancelled`, `expired`, `revoked`) require the protected candidate Brain Noise private key to be cleared in the same row update. The protected key is excluded from record representations and comparison diagnostics. Confirmation revalidates the Bridge session, principal, role, and creator account inside the transaction. A confirmed candidate can still be cancelled or revoked, which clears its key and confirmation fields. Revoking a frozen grant, installation, account, or confirming principal/session also clears the affected candidates atomically.

`confirmed` remains staging state until a separate admission transition atomically writes the durable Agent pin into `agent_pairings`. Production orchestration must authenticate window creation, verify proofs before advancing to confirmation, cancel on a second pairing request, and consume the confirmed candidate in the admission transaction. Persistence alone does not implement those transport operations.

## Transport qualification

`Noise_KK_25519_ChaChaPoly_SHA256` qualifies as a frozen-Brain transport under `tools/native-snow-brain-spike/`. Production pairing routing and session integration remain separate from persistence.

## Record sizes

Sizes come from `contracts/companion-pairing-profile/profile.json`. Routine application records use a 4,096-byte frame. `session.authorization` uses the 36,864-byte ceiling. Pairing nonces, Noise public keys, pairing digests, and grant digests are 32-byte values and are stored as bounded BLOBs.

## Grant retention and frozen bindings

`VerifiedGrantReference` retains the exact verified compact JWS only for the two companion pairing grant types. `app/persistence/auth_sql/0010_companion_grant_retention.sql` stores those retained bytes alongside the existing digest and metadata.

A window may enter `offered` only with the exact retained references for:

- `installation_grant`
- `creator_account_binding`

The window stores reference IDs and the transcript grant digest, not copied compact JWS strings. The grants must agree on issuer, subject, organization, installation and installation key, and match the active local installation key and requested account. Later staging transitions recheck those exact references for retention and currentness in the same transaction. Refresh or revocation of a frozen reference therefore blocks later progression; refreshed grants are not substituted into an already-frozen transcript.

Companion pairing requires retained grant bytes. A development database without them requires freshly verified grants before it can pair.
