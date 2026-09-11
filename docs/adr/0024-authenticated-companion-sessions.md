<!-- CODE-VERIFY: extension/transport/local-service-endpoints.mjs extension/transport/secure-local-fetch.mjs extension/transport/config-http-adapter.mjs extension/transport/agent-websocket.mjs extension/transport/chrome-adapter-core.mjs extension/qualification/snow-wasm-spike/src/lib.rs app/security/installation_key.py app/security/hosted_grants.py contracts/production/grant-profile-v1/trust-set.json -->

# ADR 0024: Protect Full-mode communication with locally paired sessions

- Status: Accepted
- Date: 2026-09-12
- Amends: [ADR 0008](0008-production-authentication.md), in the sections named under "Amendments to ADR 0008"
- Scope: Agent-to-Brain Full-mode communication. Bridge and browser-to-Brain security are outside this decision.

## Context

In Full mode, Agent sends conversation data, auth tickets, storage bootstrap material, and rotation material to Brain over loopback. Brain listens on a fixed loopback port and serves plain HTTP (ADR 0009). Any local process can occupy that port while Brain is not running. Browser-trusted HTTPS for a loopback name would need public DNS pointing at loopback, router or DNS exceptions, or trust-store changes, and none of these is an acceptable installation requirement.

ADR 0008 has Brain pair the Agent key with local confirmation. It does not have Agent authenticate Brain, and it does not protect the confidentiality or integrity of Agent traffic.

## Decision

Full mode fails closed unless Agent and Brain have an authenticated Noise session built from locally confirmed pins. No Full-mode conversation data, storage keys, auth tickets, storage bootstrap material, or rotation material may cross the Agent-to-Brain boundary outside that session. Preview stays standalone. It needs no companion, pairing, or session.

### Session

The session uses `Noise_KK_25519_ChaChaPoly_SHA256` over the loopback Agent WebSocket. Each endpoint uses the peer static key it pinned at pairing. The suite is fixed, with no negotiation or plaintext fallback. The prologue binds the session profile and the pairing digest, so a session can only run under the pairing that produced its pins.

Noise owns key derivation, authenticated encryption, directional keys, and transport nonces. The application implements no cryptographic primitives. Handshake messages carry no application payload. Both endpoints exchange fixed encrypted confirmations before admitting application records. An authentication failure destroys the session. Each reconnect runs a fresh handshake. Early data, resumption, and key update are not used.

Sessions have bounded frame size, handshake deadline, lifetime, and record count. Brain presents its current `installation_grant` and `creator_account_binding` in the first application record. Agent verifies them against its pins and the grant profile before it sends any Full-mode record.

### Pairing

Pairing is local. It uses the installation identity and grants that hosted provisioning already issues. It adds no hosted route, hosted Agent registration, or hosted pairing record.

1. An authenticated Bridge operator opens a pairing window for one approved creator account. Brain creates one pending pairing with a random pairing ID, the next local generation, a fresh Brain Noise static key pair, a fresh Brain nonce, and a short deadline.
2. Agent connects to the pairing WebSocket path and sends its Agent installation ID, its non-exportable P-256 Agent identity public key, a fresh Agent Noise static public key, and a fresh Agent nonce.
3. Brain replies with the pairing ID, generation, creator account ID, its Noise static public key, its nonce, its installation public key, its current `installation_grant` and `creator_account_binding`, and a proof. The proof is an installation-key signature over the canonical pairing transcript.
4. Agent checks that the two grants carry valid signatures from the packaged `installation-binding` trust set. It also checks that both grants are valid under the grant profile, that they agree on the organization, installation, and installation key, and that the presented installation key matches the grants' key thumbprint. The bound account must match the creator account Agent detects locally, and the proof must verify over the transcript that Agent computes itself. Agent then signs the same transcript with its identity key and returns that proof.
5. Brain verifies the Agent proof. Bridge shows a comparison code, the Agent key fingerprint, and the creator account. The Agent popup shows the comparison code derived from the same transcript.
6. The operator confirms in Bridge. Brain then commits the pin. Agent commits its pin only after a KK handshake with the pending pins succeeds and Brain's fixed session confirmation arrives.

The [companion pairing contract](../companion-pairing-contract.md) defines the transcript, proofs, comparison code, grant checks, messages, and durable state.

Pairing messages carry public keys, nonces, grants, and signatures. They carry no Full-mode secret. Agent sends only public keys and random values before it authenticates Brain. Brain returns grants only during an open window, and only to the first request. A second request during a window cancels it.

### Why Agent can trust the proof

The installation private key is non-exportable and bound to the operating-system user that provisioned Brain. The hosted plane binds its public key to the installation and creator account through grants that Agent verifies offline against a packaged trust set. A process that holds the loopback port without that key cannot produce the proof. The installation key signs pairing transcripts under their own domain, so no other installation-key signature is valid as a pairing proof.

The KK handshake then proves live possession of both pinned Noise private keys. Signed authorization alone, a health response, or a successful loopback connection never enables Full mode.

### Lifetime and revocation

A pin has no separate lease. Each session is admitted only while Brain's `installation_grant` and `creator_account_binding` stay valid under the grant profile, including its offline grace. Brain enforces this under ADR 0008. Agent enforces it again by checking the grants presented in each session. Once pinned, a pairing needs no hosted contact beyond Brain's existing grant refresh.

Revocation is local. Revoking a pairing in Bridge deletes Brain's pin and closes its sessions. Forgetting the companion in the Agent popup deletes Agent's pin. Hosted authorization loss reaches Agent through Brain and at grant boundaries. Agent has no channel to the hosted plane.

Brain allocates pairing generations monotonically per installation. Agent keeps the highest generation it has admitted for each installation and refuses lower ones. Reinstallation, loss of an identity key, or loss of a Noise key requires a new pairing. A new pairing never silently replaces an active one.

## Amendments to ADR 0008

ADR 0008 remains authoritative for separating hosted and local authority. This decision changes it only as follows.

| ADR 0008 text | Amended behavior |
| --- | --- |
| Agent runtime access: local Brain "pairs the Agent key, verifies challenges, issues Agent tickets, and performs local revocation." | Unchanged. In addition, pairing is mutual: Agent authenticates Brain by the installation-key proof and grants, and each endpoint pins the other's Noise static key. Hosted provisioning has no Agent runtime or pairing authority. |
| Local Agent authentication: "Brain shows the key fingerprint and detected creator account for local confirmation." | Bridge also shows a comparison code, and the Agent popup shows the same code. The detected creator account must equal the account bound in Brain's `creator_account_binding`. |
| Local Agent authentication: challenge, proof, and ticket issuance for Agent WebSocket and configuration access. | Unchanged in content. These exchanges run as records inside the authenticated session instead of over plain HTTP. |
| "Provisioning, grant refresh, WebAuthn, pairing, and ticket issuance use separate HTTP contracts and add no WebSocket operation." | Provisioning, grant refresh, WebAuthn, and Bridge ticket issuance keep separate HTTP contracts. Agent pairing uses a dedicated loopback WebSocket exchange. Agent challenge, ticket issuance, configuration, storage bootstrap, unseal, and rotation run inside the Agent session. |

Protocol-v1 message schemas are unchanged. They are carried inside the session.

## Why

Loopback reachability proves nothing about who is listening. The installation key is the only non-exportable local identity that the hosted plane already vouches for offline, so Agent can authenticate Brain with no new hosted authority and no hosted round trip. Local confirmation keeps a rogue local client from pairing with Brain as an Agent. Tying pin validity to the existing grants reuses the offline-grace policy that ADR 0008 already accepts.

## Consequences

- The extension talks to Brain only over the loopback WebSocket. The plain HTTP adapters, WebSocket opening headers, URL parameters, external extension messages, and error paths must not carry Full-mode secrets. The session must carry every Full-mode boundary, not just message frames.
- The manifest's local origin becomes `ws://bridge.localhost:17871`. No host permission for HTTPS or HTTP to Brain remains.
- The extension ships a WebAssembly Noise implementation. That requires `wasm-unsafe-eval` in the extension CSP and a supply-chain review of the Noise library and its build.
- Brain gains a pairing WebSocket path, per-pairing Noise keys held in its encrypted store (ADR 0019), a pairing-transcript signing purpose for the installation key, and a Bridge confirmation view.
- Same-user malware can use the installation key and read extension state. That is outside the local security boundary. A process running as a different user cannot use the key.
- A privileged attacker who restores a whole old local profile can restore old pins. Software alone cannot detect that without an operating-system or hardware monotonic anchor.
- Hosted authorization changes reach Agent only through Brain or at grant boundaries, as ADR 0008 already accepts for Brain.

## Confirmation

- Agent and Brain produce identical transcripts, proofs, comparison codes, and prologues for the vectors of contract profile `urn:bridge-clean:companion-pairing:v1`, in both JavaScript and Python. Both read the vendored copy under `contracts/`, and each verifies every file against the contract manifest and the consumer pin before using it.
- A process that holds the loopback port without the installation key cannot complete pairing or a session. It receives no Full-mode data before Agent refuses it.
- Pairing tests reject each of the following: an unknown trust-set key, wrong grant audience or type, expired grants past grace, a mismatch between grant and installation key, an account mismatch, a lower generation, a reused nonce, a second request in one window, and an expired window.
- Session tests cover replay, reordering, modification, oversize frames, deadlines, cancellation, late completion, grant expiry mid-session, revocation on each side, and standalone Preview.
- Chrome 132 and current stable Chrome qualify the service-worker WebSocket, WebAssembly loading under the extension CSP, suspension and restart, and persistence of the non-exportable identity.

## Related

- [ADR 0008: Separate hosted provisioning from local runtime authentication](0008-production-authentication.md)
- [ADR 0009: Use a local-first runtime and persistence boundary](0009-local-first-topology-and-persistence.md)
- [ADR 0019: Encrypt local Brain persistence with device-bound keys](0019-encrypted-local-persistence.md)
- [ADR 0021: Encrypt Full-mode extension persistence with a Brain-sealed key](0021-encrypted-extension-persistence.md)
- [Companion pairing contract](../companion-pairing-contract.md)
- [Noise Protocol Framework](https://noiseprotocol.org/noise.html)
- [RFC 7638: JWK thumbprint](https://www.rfc-editor.org/rfc/rfc7638.html)
