# ADR 0024: Protect Full-mode communication with authenticated sessions

- Status: proposed; authenticated pin-bootstrap contract is approved for implementation, while production transport remains gated on issuer/verifier/persistence/interoperability work.
- Scope: Agent-to-Brain Full-mode communication. Bridge/browser security is a separate architecture review.

## Security requirement

Full mode must fail closed unless Agent has established an authenticated cryptographic session with Brain that protects confidentiality and integrity. No Full-mode user data, message content, storage keys, auth tickets, storage bootstrap credentials, or rotation material may cross that boundary outside the protected session. Preview remains standalone and must not require companion availability, pairing, or session setup.

Browser-trusted HTTPS/WSS is not a requirement of this workstream. Public DNS resolving a hostname to loopback is incompatible with ordinary consumer DNS rebinding protection. Router exceptions, hosts-file entries, DNS overrides, trust-store changes, and certificate-error bypasses are not acceptable installation requirements.

## Candidate construction

Use the established Noise framework over the existing ordered loopback WebSocket transport. The isolated spike uses `Noise_KK_25519_ChaChaPoly_SHA256`: each endpoint knows the other's static public key before connecting. The suite is fixed, without negotiation or plaintext fallback. The Noise prologue binds the profile version, endpoint roles, and the verified authority-signed pairing context.

Handshake messages carry no application payload. Both endpoints exchange fixed encrypted transport confirmations before application admission. All configuration, authentication, storage-unseal, rotation, and message operations must subsequently travel in authenticated encrypted records. Existing plaintext HTTP adapters, WebSocket opening headers, URL parameters, external extension messages, and error paths must not become alternate routes for these values.

Noise owns key derivation, authenticated encryption, directional keys, and transport nonces. The application does not implement cryptographic primitives. Ordered records reject replay, reordering, modification, reflection, and cross-session reuse; any authentication failure destroys the session. Reconnection creates fresh ephemeral state and requires a new handshake. No early data, resumption, or key-update extension is selected in this proposal.

The prototype uses a 4 KiB frame cap, bounded WebSocket queues, no compression, a total connection/handshake/request deadline, cancellation, and fixed payload-free error codes. These are spike limits, not a replacement for production size budgets. A production session also needs reviewed lifetime and record-count limits, backpressure, epoch fences, and orderly key destruction. Python object disposal does not prove secret memory erasure.

## Authenticated pin provisioning

Use the dedicated hosted-authority-signed pairing receipt defined by the [companion pairing contract](../companion-pairing-contract.md). Agent and Brain independently pin a purpose-scoped `pairing-receipt` verification trust set through signed software/contract distribution. Existing installation-binding, membership, license, capability, or grant keys do not silently acquire this purpose.

The receipt binds the organization/customer namespace, logical installation and exact TPM installation signing key, logical Agent and exact enrolled Agent signing key, account, pairing lineage/generation, both independently generated X25519 public keys, both independent endpoint challenges, exact pairing grant provenance, exact customer approval revision, fixed Noise suite, purpose/audience, receipt identity/validity, and the separate offline authorization deadline.

Enrollment is a hosted two-phase transaction. After both endpoints submit authenticated public contributions, the authority freezes one immutable enrollment request. Each endpoint recomputes its domain-separated enrollment digest and signs a role-bound proof over that digest plus a separate one-time issuer challenge using its already registered identity key. Customer approval commits to the same enrollment digest. The authority revalidates proofs, current grants, approval, cancellation/revocation, and the monotonic generation under one finalization transaction before fixing/signing the receipt claim set.

Receipt delivery uses each endpoint's existing authenticated hosted provisioning channel, not an unauthenticated loopback page, pasted data, or external message. Local browser messages may only trigger an authenticated hosted status fetch. Receipt contents and Full-mode credentials are never sent on the plaintext WebSocket.

Each endpoint verifies the closed ES256 receipt profile, purpose-scoped issuer key, exact pending context, durable generation/revocation/receipt replay state, and that the receipt's own X25519 public key is derived from the locally held candidate private-key handle. Admission atomically consumes pending state and installs the peer pin. Cancellation/revocation must fence a concurrent verifier result.

Verified receipt claims are encoded with a fixed domain-separated length-prefixed binary profile and SHA-256 into the Noise prologue. JWS signature bytes and issuer `kid` are excluded so re-signing/key rotation over the same verified authorization tuple cannot split the Noise transcript. A published Python vector is part of the browser interoperability gate.

After pin admission, a loopback port owner must still prove live possession of the authorized Noise private key in a fresh KK handshake. Signed authorization alone, a health response, or successful loopback connection never enables Full mode. This separates authorization from live key possession and removes trust on first use.

## Pairing lifetime, revocation, and rollback

Receipt `exp` bounds receipt admission only. An admitted pin can be used only until the separately signed `offline_not_after`, and a session may not outlive the remaining offline lease. Known hosted revocation closes active sessions and prevents reconnect immediately. While offline, no endpoint can learn a newly created hosted revocation, so production must choose a finite offline lease consistent with the grant/revocation policy.

The hosted authority is the sole generation allocator per random `pairing_id` lineage and durably stores highest-issued and revoked-through generations. Endpoints transactionally persist the active lineage, highest admitted generation, revocation floor/tombstones, consumed receipt IDs, exact identity/account/approval/grant metadata, own Noise-key reference, peer pin, and binding digest. A higher generation is admitted only through a fresh authenticated pending replacement with new endpoint challenges, new proof challenges, current grants, and fresh approval. A different lineage or reinstall is a new pairing, never an implicit key rollover.

Ordinary local replay/rollback is rejected by durable high-water/revocation state. A privileged attacker able to restore an entire old local database/profile can also restore its local high-water mark; pure software cannot prove monotonicity against that attacker while indefinitely offline. The contract therefore relies on authoritative hosted generation/revocation state plus a finite signed offline deadline. Stronger privileged-local rollback resistance would require an OS/hardware monotonic anchor.

## Implementation findings

`extension/transport/local-service-endpoints.mjs` currently enforces HTTPS/WSS. `secure-local-fetch.mjs` bounds HTTP operations. `config-http-adapter.mjs` carries an auth ticket in an HTTP header, while `chrome-adapter-core.mjs` handles storage bootstrap, unseal, and rotation. `agent-websocket.mjs` carries session tickets. These paths and their read-only counterparts must be reviewed together; encrypting message frames alone would leave Full-mode secrets outside the session.

No production transport or permission change is authorized by this ADR revision. The current fail-closed implementation remains until the issuer, production endpoint verification/persistence, and actual Noise transport migration pass their respective gates. Bridge UI origin, cookies, WebAuthn, and browser-to-Brain sessions remain outside this proposal.

## Evidence and production gate

The [isolated Python spike](../../tools/companion-session-spike/README.md) tests strict synthetic pairing receipts, exact context/key admission, generation/revocation inputs, canonical proof/binding vectors, and Noise records over `ws://127.0.0.1:17871`. A separate hostile process occupies that port and must fail to obtain application plaintext or activate a session. Test fixtures do not establish production issuer behavior, production trust anchors, MV3 persistence, or actual hosted grant validation.

The [Agent pairing components](../../extension/qualification/companion-pairing.md) add receipt verification against the hosted interoperability fixture, non-exportable Agent identity, transactional IndexedDB pin storage, and an Agent Snow session adapter. MV3 persistence/race tests and real-WASM lifecycle tests cover these components separately. They remain disconnected from shipping composition until hosted Agent authentication, trust-set release inputs, Brain integration, and the production gates below are resolved.

Before production transport integration, require:

1. Hosted issuer implementation of the exact pairing contract: dedicated signing purpose/trust-set rotation, Agent identity enrollment, TPM/Agent proof profiles, one-time challenges, grant digest, explicit approval, atomic generation/finalization, protected retrieval, cancellation/revocation/reinstall/replacement, and finite offline authorization.
2. Production Agent and Brain verifiers with transactional durable pending/admitted state, protected identity/Noise key handling, highest-generation/revocation persistence, and cancellation/account/consent commit fences.
3. A maintained browser-compatible Noise implementation with reviewed supply-chain provenance and published byte-for-byte Python/MV3 vectors for receipt verification, enrollment/proof transcripts, prologue binding, handshake roles, and negative cases.
4. Chrome 132 and current Chrome tests of actual extension service-worker WebSocket permissions, loopback access, Local Network Access behavior, suspension/restart, non-exportable Agent identity persistence, and hostile-port ownership. No DNS or browser-policy bypasses are allowed.
5. An audit of every Full-mode boundary and release tests covering zero secret egress before confirmation, deadlines, cancellation, frame/response limits, replay, downgrade, stale generation/revocation state, offline expiry, late completions, and standalone Preview.

## Sources

- [Noise Protocol Framework](https://noiseprotocol.org/noise.html): handshake patterns, prologues, authentication properties, and transport cipher state.
- [Python Noise implementation](https://github.com/plizonczyk/noiseprotocol): implementation used only by the spike; use here is not a production endorsement.
- [RFC 7515](https://www.rfc-editor.org/rfc/rfc7515.html): compact JWS and ES256 signature encoding.
- [RFC 7638](https://www.rfc-editor.org/rfc/rfc7638.html): JWK thumbprints used to bind signing identities.
- [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725.html): explicit typing, algorithm verification, key-source restrictions, and mutually exclusive validation rules.
