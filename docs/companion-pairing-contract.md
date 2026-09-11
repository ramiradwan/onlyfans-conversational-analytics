# Authenticated companion pairing contract

This contract supplies the non-TOFU trust bootstrap for [ADR 0024](adr/0024-authenticated-companion-sessions.md). The Python spike is a verifier/admission model and interoperability fixture only; it is not the hosted issuer, production persistence, or a production transport change.

## Security invariant and exact identities

A loopback listener is untrusted until it proves possession of the Brain X25519 private key authorized by a receipt independently verified by Agent and Brain against pinned hosted authority keys. A key learned from port `17871`, a health response, sender URL, local page, or extension message never establishes identity.

No Full-mode auth ticket, storage key/bootstrap value, reconnect credential, conversation data, rotation secret, receipt, or account identifier crosses the unauthenticated loopback boundary. Before Noise completes, loopback carries only the bounded Noise handshake and encrypted confirmation records.

Every pairing receipt binds this exact tuple:

- `organization_id`: customer/tenant namespace;
- `installation_id`, `installation_key_id`, `installation_key_jkt`: logical Brain installation plus the exact registered TPM-backed P-256 signing key and RFC 7638 SHA-256 thumbprint;
- `agent_id`, `agent_identity_key_id`, `agent_identity_key_jkt`: logical Agent plus the exact enrolled P-256 Agent signing key and thumbprint;
- `account_id`: authorized creator account;
- `pairing_id`: random 256-bit lineage identifier, lowercase hex;
- `generation`: hosted-authority monotonic generation within that lineage.

The Agent identity is a dedicated non-exportable WebCrypto P-256 ECDSA key registered through authenticated hosted provisioning. It is not an auth ticket, storage/Noise key, Bridge credential, or other Full-mode secret. Replacing either endpoint signing identity requires fresh pairing approval; an old receipt cannot authorize a replacement key.

## Enrollment and issuance ceremony

Pairing is a hosted two-phase transaction:

1. Agent independently generates a new X25519 key pair and random 256-bit `agent_nonce`; Brain independently generates its X25519 key pair and random 256-bit `brain_nonce`.
2. Each endpoint durably records authenticated pending state: own candidate private-key reference/public key, own nonce, selected identities/account, lineage/generation, approval/grant context, and deadline. Hosted values may be compared with this state but never overwrite it.
3. Each endpoint submits only public contribution material through its already authenticated hosted provisioning channel. Noise private keys never leave the endpoint.
4. After both contributions exist, the authority freezes one immutable enrollment request containing the complete receipt tuple except authority-assigned `jti`, `iat`, and `exp`.
5. The authority returns that request over each authenticated provisioning channel plus a separate random 256-bit one-time proof challenge. Each endpoint recomputes the enrollment digest, verifies its own contribution and expected identities/context, then signs only if they match.
6. Brain signs with the exact registered TPM installation key. Agent signs with the exact registered Agent identity key.
7. Explicit customer approval is recorded against the same enrollment digest, `pairing_id`, and generation. Approval created before both endpoint contributions are fixed is invalid.
8. In one finalization transaction the authority revalidates both proofs/challenges, current grants, account/installation authorization, approval revision, cancellation/revocation state, and generation; consumes the request/challenges; fixes one immutable receipt claim set; and advances authoritative generation state before signing.
9. A signing retry may create another ES256 signature over the same finalized claims, but never a different claim set for that request.
10. Agent and Brain retrieve the receipt only through authenticated hosted provisioning. A local message may trigger a hosted poll but cannot carry a receipt, approve a request, alter pending expectations, or install a pin.
11. Each endpoint verifies the receipt against its pending state and atomically consumes pending state while installing the pin and advancing durable replay/rollback state.
12. A fresh Noise KK handshake must still prove live possession of both authorized X25519 private keys. Signed authorization alone never opens Full mode.

Changing either Noise key, endpoint nonce, identity binding, account, approval revision, grant context, lineage/generation, suite, or offline deadline creates a different enrollment digest and requires new proofs. Key/identity replacement additionally requires fresh customer approval.

## Canonical enrollment and possession proofs

Proof transcripts use a domain followed by a sequence of `uint32_be(length) || field_bytes`. Strings are UTF-8; integers are unsigned 64-bit big-endian; keys/challenges/digests are raw bytes. No JSON serialization participates.

`enrollment_digest = SHA-256(...)` with domain `OFCA-COMPANION-PAIRING-ENROLLMENT-V1\0` and fields in this exact order:

`audience`, `suite`, `organization_id`, `installation_id`, `installation_key_id`, `installation_key_jkt`, `agent_id`, `agent_identity_key_id`, `agent_identity_key_jkt`, `account_id`, `pairing_id`, raw Agent X25519 key, raw Brain X25519 key, raw `agent_nonce`, raw `brain_nonce`, `generation`, raw `grant_digest`, `approval_id`, `approval_revision`, `offline_not_after`.

The possession-proof message uses domain `OFCA-COMPANION-PAIRING-PROOF-V1\0` and fields:

`pairing-enrollment`, audience `urn:ofca:companion-pairing:v1`, role (`agent` or `brain`), raw 32-byte issuer challenge, raw 32-byte `enrollment_digest`, identity-key ID.

Both proofs are ES256 over those exact bytes with canonical 64-byte P1363 `r || s` and low-S normalization. Brain uses the existing TPM-backed installation-key abstraction; Agent uses its registered non-exportable WebCrypto P-256 key. The authority resolves the proof key from previously registered identity state, not from a key supplied only in the enrollment request.

Issuer proof challenges are distinct from `agent_nonce`/`brain_nonce`, role/request scoped, expire no later than the enrollment request, and are durably one-time consumed. Reused challenges and identical Agent/Brain endpoint nonces are refused.

## Grant context and customer approval

`grant_digest` is the exact pairing authorization provenance for `AGENT_PAIRING_GRANT_TYPES`: `creator_account_binding` and `installation_grant`.

For each required type, take its existing lowercase 64-hex `VerifiedGrantReference.grant_digest` (SHA-256 of the exact verified compact grant), sort pairs by ASCII grant type, and compute SHA-256 over domain `OFCA-COMPANION-PAIRING-GRANTS-V1\0` followed for each pair by length-prefixed ASCII grant type and length-prefixed raw 32-byte decoded digest. Missing, duplicate, extra, expired, revoked, installation-mismatched, or account-mismatched grants make enrollment ineligible.

This digest records issuance provenance; it does not replace runtime authorization. Routine grant refresh after admission does not itself replace a pin. Full-mode operations still evaluate current grants/consent. Removal of pairing authority, an account/installation change, or approval revocation updates pairing revocation state and fences sessions under the online/offline policy.

Customer approval is a durable hosted record containing at least `approval_id`, monotonic `approval_revision`, approver principal, `pairing_id`, generation, enrollment digest, exact approved installation/Agent/account tuple, `approved_at`, and cancellation/revocation state. The receipt signs the current unrevoked approval ID/revision; changing approval-critical data creates a new revision/enrollment.

## Lineage, cancellation, revocation, recovery, and offline use

The hosted authority is the sole generation allocator. `generation` starts at 1 and strictly increases per `pairing_id`; the authority transactionally stores highest-issued and `revoked_through_generation`. A finalized request cannot reuse/decrement a generation.

Endpoints persist highest admitted generation and revocation floor. Admission rejects generations at/below either floor, rejects a consumed `jti`, and never lets same-generation delivery replace a pin. A higher generation requires a separately authenticated pending replacement with fresh endpoint nonces, fresh issuer proof challenges, current grants, and current approval. A different `pairing_id` is a new pairing and cannot silently replace the active lineage.

Hosted enrollment states are explicit: `pending`, `approved`, `issued`, `cancelled`, `revoked`. Finalization is transactional/CAS; cancellation or revocation observed before commit wins over concurrent verification/signing. Cancellation consumes/fences outstanding proof challenges. Post-issuance revocation records an authoritative floor/tombstone, prevents retrieval/admission/reissue at lower/equal generation, and closes known active sessions when delivered. Deleting a local pin never clears hosted generation/revocation state.

Reinstall, lost identity key, or lost Noise private key requires a new pairing. Recovery never trusts the process currently listening on loopback and never treats an old receipt as proof of a new key.

Receipt `exp` bounds **receipt admission only**. `offline_not_after` separately bounds creation/continuation of sessions while current hosted pairing state is unavailable; a session cannot outlive the remaining lease. Known revocation closes sessions immediately. While offline, endpoints cannot learn a new hosted revocation, so production must choose a finite offline lease. Extending it requires fresh authenticated hosted authorization, never local clock passage or replay of the old receipt.

## Pairing receipt JWS profile

The artifact is compact ES256 JWS. Protected header fields are exactly:

- `alg`: `ES256`;
- `typ`: `ofca-companion-pairing+jwt`;
- `kid`: bounded local identifier selecting an already pinned key whose sole purpose is `pairing-receipt`.

No other header is accepted. Embedded/remote key references (`jku`, `jwk`, `x5u`, `x5c`), critical extensions, and algorithm negotiation are forbidden. A receipt never supplies its own trust anchor.

The payload contains exactly these claims and no others:

| Claim | Requirement |
| --- | --- |
| `iss` | Exact configured pairing issuer. |
| `aud` | Exact string `urn:ofca:companion-pairing:v1`; arrays rejected. |
| `iat`, `exp` | Safe integers; `iat <= now < exp <= iat + 300`. |
| `jti` | Random 256-bit lowercase-hex receipt ID. |
| `suite` | Exact `Noise_KK_25519_ChaChaPoly_SHA256`. |
| `organization_id` | Exact tenant/customer. |
| `installation_id`, `installation_key_id`, `installation_key_jkt` | Exact Brain installation/signing identity. |
| `agent_id`, `agent_identity_key_id`, `agent_identity_key_jkt` | Exact Agent/signing identity. |
| `account_id` | Exact authorized creator account. |
| `pairing_id` | Random 256-bit lowercase-hex lineage. |
| `agent_key`, `brain_key` | Canonical unpadded base64url of exactly 32 raw X25519 bytes. |
| `agent_nonce`, `brain_nonce` | Independent 256-bit lowercase-hex challenges; must differ. |
| `generation` | Safe positive authority-monotonic integer. |
| `grant_digest` | Exact pairing grant digest above, lowercase hex. |
| `approval_id`, `approval_revision` | Exact current approval record/revision. |
| `offline_not_after` | Safe integer, not earlier than `exp`. |

Identifier fields use ASCII `[A-Za-z0-9][A-Za-z0-9._~-]{0,127}`. JWK thumbprints are canonical unpadded 43-character base64url encodings of 32 bytes. Numeric values are integers (not bool/float) below `2^53`.

Compact JWS is at most 8 KiB; decoded protected header at most 512 bytes; decoded payload at most 6144 bytes. All segments use canonical unpadded base64url. Header/payload are UTF-8 JSON objects with unique member names; duplicates are rejected before key selection/semantic use. Signature is exactly 64-byte P1363 ES256 and low-S; malformed/high-S signatures are rejected. Replay decisions use `pairing_id`/generation/`jti`/pending state, never concrete JWS or signature bytes.

## Issuer trust and key rotation

Pairing receipts use a new signing key purpose. Existing installation-binding, membership, license, capability, or grant keys do not acquire pairing authority.

Production distributes a `pairing-receipt-v1` trust set to Agent and Brain through signed software/contract distribution. Entries are P-256 with purpose `pairing-receipt`, stable `kid`, and checked RFC 7638 thumbprint. `kid` only selects among local trusted entries.

Rotation publishes the new verification key before use, permits a bounded overlap, then retires the old signer. Admitted pins store issuer `kid` as provenance but `kid` is not in the Noise binding, so identical verified claims re-signed during rotation retain the same Noise context. Emergency key compromise must support revoking still-live pairings issued by that key or shortening them through authenticated revocation policy. Unknown/retired/wrong-purpose keys are rejected.

## Endpoint admission and durable state

Verification is read-only until final commit. Each endpoint must:

1. verify the closed JWS profile and purpose-scoped issuer key;
2. compare every receipt context field to independently authenticated pending state;
3. derive its own X25519 public key from the locally held candidate private-key handle and require an exact receipt match;
4. enforce durable lineage/generation/revocation/consumed-`jti`/approval/grant/time rules;
5. derive the canonical Noise binding from verified typed claims;
6. atomically consume pending state, install the peer pin, advance replay/high-water state, and store issuer provenance;
7. refuse commit if cancellation, account/consent change, revocation, or another state transition changed the pending version during verification.

Agent and Brain durably retain at least: active `pairing_id`, generation, receipt `jti`, issuer `kid`/trust-set version; exact organization/installation/Agent/account and signing-key identities; own Noise private-key reference/public key and peer pin; Noise binding digest; `grant_digest`, approval ID/revision, receipt `iat`/`exp`, `offline_not_after`; highest generation, revocation floor/tombstones, required consumed receipt IDs; and, if pending enrollment survives restart, its request ID/version, candidate key reference/public key, endpoint nonce, expected identity/account/grant/approval context, expected next generation, and deadline.

Brain uses a durable DB transaction. Agent uses a transactional browser store (for example IndexedDB) and a non-exportable WebCrypto Agent identity; sequential best-effort `chrome.storage` writes are insufficient for admission. Noise handshake/cipher state and transport nonces never survive restart.

Authoritative hosted generation/revocation state plus transactional local high-water state prevents ordinary replay/rollback. A privileged attacker restoring an entire old local image can also restore its local high-water mark; pure software cannot prove monotonicity against that attacker indefinitely offline. The bounded signed `offline_not_after` limits that case. Stronger privileged-local rollback resistance requires an OS/hardware monotonic anchor and is outside this contract.

## Noise prologue binding and Full-mode boundary

After complete JWS verification, both endpoints SHA-256 one semantic binary encoding into the Noise prologue. It uses domain `OFCA-COMPANION-PAIRING-BINDING-V1\0`, the same length framing above, and this exact order:

`typ`, `iss`, `aud`, `iat`, `exp`, `jti`, `suite`, `organization_id`, `installation_id`, `installation_key_id`, `installation_key_jkt`, `agent_id`, `agent_identity_key_id`, `agent_identity_key_jkt`, `account_id`, `pairing_id`, raw Agent X25519 key, raw Brain X25519 key, raw `agent_nonce`, raw `brain_nonce`, `generation`, raw `grant_digest`, `approval_id`, `approval_revision`, `offline_not_after`.

The spike contains fixed enrollment/proof/grant/binding vectors that MV3 and Python implementations must reproduce. JWS signature bytes and `kid` are deliberately excluded: the signature proves authority; the verified semantic tuple is the Noise context. Re-signing/key rotation over identical claims must not split the transcript.

`Noise_KK_25519_ChaChaPoly_SHA256` is fixed, has no negotiation/plaintext fallback, and starts only from receipt-installed pins. The subsequent handshake proves live key possession; encrypted fixed confirmations precede application admission. Current grants/consent/operation authorization still apply after cryptographic authentication.

> Full mode fails closed until an authenticated Noise session has been established using pins installed from a valid authority-signed pairing receipt. No Full-mode conversation data, storage keys, auth tickets, storage-bootstrap material, credential-rotation data, or equivalent secrets may traverse the extension/companion boundary outside that protected session.

Preview remains standalone and requires neither companion, issuer, pairing, nor Noise.

## Production implementation gate

Production transport remains unchanged until all four are complete and interoperable:

1. hosted issuer implementing the exact identity enrollment, proof/challenge, approval/grant digest, generation/finalization, receipt/trust rotation, retrieval, cancellation/revocation, recovery/replacement, and offline-lease contracts;
2. Agent/MV3 verifier with registered non-exportable Agent identity, transactional pending/admitted state, durable replay/high-water/revocation state, and vectors;
3. Brain enrollment/verifier using the registered TPM identity, protected Noise key storage, durable pin/high-water/revocation state, and the same vectors;
4. actual Full-mode Noise integration plus release tests proving zero secret egress before confirmation, no downgrade/fallback, cancellation/late-completion fencing, hostile-port refusal, current Chrome behavior, and standalone Preview.
