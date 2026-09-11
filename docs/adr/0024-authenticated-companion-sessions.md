# ADR 0024: Protect Full-mode communication with authenticated sessions

- Status: proposed; cryptographic profile and trust bootstrap require review before production integration.
- Scope: Agent-to-Brain Full-mode communication. Bridge/browser security is a separate architecture review.

## Security requirement

Full mode must fail closed unless Agent has established an authenticated cryptographic session with Brain that protects confidentiality and integrity. No Full-mode user data, message content, storage keys, auth tickets, storage bootstrap credentials, or rotation material may cross that boundary outside the protected session. Preview remains standalone and must not require companion availability, pairing, or session setup.

Browser-trusted HTTPS/WSS is not a requirement of this workstream. Public DNS resolving a hostname to loopback is incompatible with ordinary consumer DNS rebinding protection. Router exceptions, hosts-file entries, DNS overrides, trust-store changes, and certificate-error bypasses are not acceptable installation requirements.

## Candidate construction

Evaluate the established Noise framework over the existing ordered loopback WebSocket transport. The isolated spike uses `Noise_KK_25519_ChaChaPoly_SHA256`: each endpoint knows the other's static public key before connecting. The suite is fixed, without negotiation or plaintext fallback. The Noise prologue binds the profile version, endpoint roles, and a pre-established pairing context.

Handshake messages carry no application payload. Both endpoints exchange fixed encrypted transport confirmations before application admission. All configuration, authentication, storage-unseal, rotation, and message operations must subsequently travel in authenticated encrypted records. Existing plaintext HTTP adapters, WebSocket opening headers, URL parameters, external extension messages, and error paths must not become alternate routes for these values.

Noise owns key derivation, authenticated encryption, directional keys, and transport nonces. The application does not implement cryptographic primitives. Ordered records reject replay, reordering, modification, reflection, and cross-session reuse; any authentication failure destroys the session. Reconnection creates fresh ephemeral state and requires a new handshake. No early data, resumption, or key-update extension is selected in this proposal.

The prototype uses a 4 KiB frame cap, bounded WebSocket queues, no compression, a total connection/handshake/request deadline, cancellation, and fixed payload-free error codes. These are spike limits, not a replacement for production size budgets. A production session also needs reviewed lifetime and record-count limits, backpressure, epoch fences, and orderly key destruction. Python object disposal does not prove secret memory erasure.

## Authenticated pin provisioning

Use a dedicated hosted-authority-signed pairing receipt, verified independently by Agent and Brain. The [pairing contract proposal](../companion-pairing-contract.md) defines issuance prerequisites, the protected delivery path, signed bindings, and admission rules. This is a new purpose-specific artifact; existing installation and account grants are not reinterpreted or changed by the spike.

The receipt binds both X25519 keys to the installation, Agent, account, independent fresh endpoint challenges, pairing generation, authorization digest, and fixed Noise suite. Each endpoint compares its own signed key with its locally generated key and the receipt context with its independently authenticated pending pairing state. The issuer must verify the TPM-backed installation proof, Agent key possession, current grants, and explicit customer approval before signing.

Receipt delivery uses the existing authenticated hosted provisioning channel, not an unauthenticated loopback page or external message. Receipt contents and Full-mode credentials are never sent on the plaintext WebSocket. Each endpoint derives the same Noise prologue binding from the verified receipt claims. A port owner must then prove possession of the authorized Noise private key before any application data is admitted. This separates signed authorization from live proof of possession and avoids trust on first use.

The spike implements ES256 JWS verification through PyJWT, strict context/key checks, atomic in-memory pin admission, replay refusal, and cancellation/revocation fences. Tests use a synthetic hosted authority and signed receipts to establish Noise sessions. They do not establish actual hosted issuance or production grant validation. Production integration remains gated on cross-plane contract review, the real issuer and extension verifier, durable pin state, and offline authorization policy.

## Implementation findings

`extension/transport/local-service-endpoints.mjs` currently enforces HTTPS/WSS. `secure-local-fetch.mjs` bounds HTTP operations. `config-http-adapter.mjs` carries an auth ticket in an HTTP header, while `chrome-adapter-core.mjs` handles storage bootstrap, unseal, and rotation. `agent-websocket.mjs` carries session tickets. These paths and their read-only counterparts must be reviewed together; encrypting message frames alone would leave Full-mode secrets outside the session.

No production transport or permission change is authorized by this proposal. The current fail-closed implementation remains until the construction, bootstrap, and migration boundaries pass review. Bridge UI origin, cookies, WebAuthn, and browser-to-Brain sessions remain outside this proposal.

## Evidence and acceptance gate

The [isolated Python spike](../../tools/companion-session-spike/README.md) tests signed synthetic pairing receipts, pin admission, and Noise records over `ws://127.0.0.1:17871`. A separate hostile process occupies that port and must fail to obtain application plaintext or activate a session. Test fixtures do not establish production pin provenance.

Before production integration, require:

1. Cryptographic review of the chosen pattern, transcript/context binding, role admission, confirmation, replay behavior, lifetime limits, and downgrade refusal.
2. Review and implementation of authenticated provisioning, pin persistence, key delegation/protection, pairing approval, revocation, and rotation.
3. A maintained browser-compatible implementation with reviewed supply-chain provenance, published vector tests, and Python/browser interoperability. The Python spike is not evidence of MV3 compatibility or a library audit.
4. Chrome 132 and current Chrome tests of actual extension service-worker WebSocket permissions, loopback access, Local Network Access behavior, suspension/restart, and hostile-port ownership. No DNS or browser-policy bypasses are allowed.
5. An audit of every Full-mode boundary and release tests covering zero secret egress before confirmation, deadlines, cancellation, frame/response limits, replay, downgrade, late completions, and standalone Preview.

## Sources

- [Noise Protocol Framework](https://noiseprotocol.org/noise.html): handshake patterns, prologues, authentication properties, and transport cipher state.
- [Python Noise implementation](https://github.com/plizonczyk/noiseprotocol): implementation used only by the spike; use here is not a production endorsement.
