# Brain companion-pairing implementation map

<!-- CODE-VERIFY: Check app/persistence/auth.py, auth_sql/0010_companion_grant_retention.sql, app/security/hosted_grants.py, grant_types.py, and the vendored companion profile for retention ownership and bounds. -->

This note maps ADR 0024 and the vendored `urn:bridge-clean:companion-pairing:v1` profile onto the Brain security and storage owners that exist today. It describes ownership only; it does not change runtime behavior.

## Existing security/storage owners

- **Installation key:** `app/security/installation_key.py`. `InstallationKeyAuthority` coordinates the durable `InstallationKeyReference` in `AuthenticationStore` with `InstallationKeyProvider`; production uses `WindowsCNGInstallationKeyProvider` fixed to the Microsoft Platform Crypto Provider. The provider permits signing only and rejects exportable/non-hardware keys. `sign_challenge()` hashes caller-provided canonical bytes with SHA-256 and returns ES256 as 64-byte P1363. `_canonical_es256_signature()` validates P-256 `r,s` and normalizes low-S. Hosted provisioning supplies its own domain separation in `app/security/hosted_grants.py` with `BRIDGE-CLEAN-INSTALLATION-PROOF-V1\0` before invoking `sign_challenge()`.
- **Hosted grant verification and refresh:** `app/security/hosted_grants.py` owns acquisition/refresh orchestration; `app/security/grant_verifier.py` owns compact-JWS verification against the pinned trust set; `app/persistence/auth.py` owns `VerifiedGrantReference` and durable current/revocation state. Grace is folded into `VerifiedGrantReference.expires_at` by `_verified_reference()`, so existing ADR 0008 currentness checks operate on usable-until including grace.
- **`auth.sqlite3` transactions:** `app/persistence/auth.py` (`SQLiteAuthenticationStore`) is the typed authority; schema evolution is `app/persistence/auth_sql/*.sql`; `app/persistence/database.py` (`AuthSQLite` / `LocalSQLite.transaction`) opens SQLCipher connections and uses `BEGIN IMMEDIATE` by default, commits on success, and rolls back on any exception.
- **ADR 0019 protected storage:** `app/persistence/database.py` derives the SQLCipher `auth` key via `app/security/local_data_key.py`. The latter also exposes `protect_local_secret()` / `unprotect_local_secret()` with purpose-bound DPAPI CurrentUser protection (and the explicit test-only AES-GCM path). This is the existing mechanism for wrapping a 32-byte Brain X25519 private key before its ciphertext is retained in `auth.sqlite3`; no new key hierarchy is required.
- **Bridge authentication/session state:** `app/api/security.py` resolves the Bridge cookie into a current durable Bridge session in `SQLiteAuthenticationStore`, builds a `RuntimePolicy`, and supplies existing same-origin and CSRF checks. `app/security/webauthn.py` plus `app/api/endpoints/webauthn.py` issue/verify durable WebAuthn sessions. Pairing confirmation reuses these identities and account boundaries and must not weaken `verify_same_origin()` or `verify_csrf_token()`.
- **WebSocket routing:** `app/main.py` includes `app/api/endpoints/transport_ws.py`; that router owns `/ws/agent` and `/ws/bridge`. The dedicated `/ws/agent/pairing` route belongs beside them, while durable admission decisions remain in `SQLiteAuthenticationStore` rather than socket-local state.
- **Agent pairing records, challenges and tickets:** `app/persistence/auth.py` already owns the durable `AgentPairing` record and the `agent_pairings` table, together with `AgentChallengeBinding`, `runtime_tickets`, grant bindings, revocation snapshots, one-time consumption, and authorization-epoch fencing. `RevocationScopeType.AGENT_PAIRING` already scopes revocation to `agent_pairings.pairing_id`. Separately, `app/transport/manager.py` contains the shipping legacy in-memory pairing-ticket/HMAC reconnect/config-ticket path used by the existing Full-mode transport; that cutover is out of scope here.
- **Shutdown/revocation socket fencing:** `app/transport/manager.py` owns active Agent/Bridge socket registries, disconnects, lease retirement, and socket close behavior; `app/main.py` calls `transport_manager.stop()` on application shutdown. `SQLiteAuthenticationStore.revoke()` atomically advances a revocation scope, invalidates bound Bridge sessions/challenges/tickets, and increments the authorization epoch. Companion-session lifecycle fencing connects that durable revocation event to TransportManager-owned active Noise sockets.

## Companion-pairing ownership if implementation proceeds

`AgentPairing` in `app/persistence/auth.py` is the single pairing authority. Companion pairing extends that record, its table and its store methods; it does not introduce a second pairing record, a second pairing table, a parallel authentication database, or a second installation-key abstraction. `register_agent_pairing()` and `activate_agent_pairing()` gain the companion fields, and `_require_pairing_current()` keeps deciding currentness for both paths.

| State/capability | Owner |
| --- | --- |
| pairing window state and CAS version | `AgentPairing` in `app/persistence/auth.py` + `app/persistence/auth_sql/0011_*.sql` extending `agent_pairings` |
| installation `highest_pairing_generation` | `app/persistence/auth.py` + the same auth migration |
| wrapped Brain Noise private key | wrapper: `app/security/local_data_key.py`; ciphertext/state: `app/persistence/auth.py` + the same auth migration |
| durable Agent pin | the existing `AgentPairing.public_key` / `key_fingerprint` columns |
| confirming Bridge principal/session | `app/persistence/auth.py` + the same auth migration, populated from the `app/api/security.py` authenticated policy |
| pairing state-machine orchestration | `app/security/companion_pairing.py` (new), holding no authority: every transition delegates to an `SQLiteAuthenticationStore` CAS method |
| pairing WebSocket adapter | `app/api/endpoints/transport_ws.py` (or a sibling endpoint module included by `app/main.py`), with no authoritative in-memory admission state |
| Noise handshake/record codec | `app/security/companion_noise.py` (new), using an upstream Noise implementation; no persistence of handshake/cipher/nonces |
| active authenticated Noise sessions and close fencing | `app/transport/manager.py`; authenticated context created only after the Noise/session-authorization gate |

## Transport qualification gate

`Noise_KK_25519_ChaChaPoly_SHA256` qualifies as a frozen-Brain transport under `tools/native-snow-brain-spike/`: an upstream Snow build that survives PyInstaller freezing, interoperates with the extension's MV3 WASM peer, agrees with an independent oracle, and rebuilds to identical bytes from the pinned lock. [Run 34678991700](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/34678991700) provides that evidence at `e279340af9e9ec2e8d51bd571992bb9588bcbeb5`. Production pairing routing and session integration remain separate implementation steps.

## Record sizes

Sizes come from `contracts/companion-pairing-profile/profile.json` and are recomputed from its derivation inputs rather than restated in code. A record is a one-byte type discriminator plus payload under a 16-byte AEAD tag, so a frame bounds its plaintext at frame minus 17. Routine application records carry 4,079 plaintext bytes in a 4,096-byte frame. The single `session.authorization` record carries 36,847 plaintext bytes and fills the 36,864-byte ceiling exactly; the widest permitted envelope, two grants at the 16,384-character limit, encodes to 32,853 bytes and therefore fits. Brain keys its inbound bound off the message type, as the Agent and the Python reference already do.

## Grant-byte retention required before pairing admission

`VerifiedGrantReference` retains the SHA-256 digest and, for the two companion grant types, the exact verified compact JWS required by `pair.offer` and encrypted `session.authorization`. Other hosted grant types retain only their existing metadata and digest.

`app/persistence/auth_sql/0010_companion_grant_retention.sql` adds a nullable compact-JWS column to the SQLCipher-protected `auth.sqlite3` schema. Acquisition and refresh persist it atomically with the verified digest and metadata, under these bounds:

- Only ASCII compact JWS is retained, capped at the contract's 16,384-character grant limit. The store verifies that its SHA-256 matches the recorded digest.
- The stored column is secret material: it is redacted from logs, diagnostics, support bundles and any export path, which carry the existing `grant_digest` instead.
- It is cleared when the grant is superseded by refresh and when its revocation scope advances, so the retained set never outlives the current grant.
- Provisioning replay preserves retention state and cannot restore bytes cleared by revocation. A fresh replacement grant is required.

Pre-migration rows remain usable for existing ADR 0008 behavior but are not eligible for companion pairing until refreshed, because a digest cannot reconstruct the wire token. This stays within the existing local authorization-storage boundary and needs no hosted pairing endpoint and no second security database.

The companion-grant eligibility predicate checks retention and currentness. Final pairing admission must also recheck the existing account and identity constraints in its own transaction. The future pairing-state migration is `0011_*.sql`; retention does not install a companion route or change existing ADR 0008 pairing behavior.
