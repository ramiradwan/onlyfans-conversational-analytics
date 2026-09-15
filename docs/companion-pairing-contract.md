<!-- CODE-VERIFY: app/security/installation_key.py app/security/grant_verifier.py app/security/hosted_grants.py contracts/grant-profile-v1 contracts/production/grant-profile-v1/trust-set.json extension/runtime/companion-agent-identity.mjs extension/runtime/companion-pairing-store.mjs extension/transport/pairing-contract.mjs extension/transport/companion-noise-session.mjs extension/qualification/snow-wasm-spike/src/lib.rs -->

# Companion pairing contract

This document explains local Agent-to-Brain pairing and session binding for [ADR 0024](adr/0024-authenticated-companion-sessions.md). Both endpoints are local. The hosted plane takes no part beyond issuing the grants Brain already holds.

The normative definition is the versioned contract profile `urn:bridge-clean:companion-pairing:v1`, vendored under `contracts/companion-pairing-profile/profile.json` with its message schemas under `contracts/schemas/local/v1/` and its test vectors under `contracts/companion-pairing-v1/`. That record fixes the domain strings, field order, proof profile, comparison-code and grant-digest constructions, prologue, and limits; `contracts/manifest.json` and `contracts/consumer-pin.json` pin every vendored byte. This document describes the same design in prose and cites the record where a value matters. Where the two disagree, the vendored contract governs and this document is wrong.

## Invariant

A loopback listener is untrusted until it proves possession of the installation key bound by valid grants, and then possession of the Noise static key that key signed. A key learned from the loopback port, a health response, a sender URL, a local page, or an extension message never establishes identity.

Before a session is established, loopback carries only the pairing messages below and Noise handshake messages. No auth ticket, storage key, bootstrap value, reconnect credential, rotation secret, or conversation data crosses it.

## Keys and identities

| Key | Holder | Protection | Use |
| --- | --- | --- | --- |
| Installation key, P-256 | Brain | Non-exportable operating-system key, bound to the provisioning user | Signs the Brain pairing proof. Its RFC 7638 thumbprint is `installation_key_jkt` in the grants. |
| Agent identity key, P-256 | Agent | Non-exportable WebCrypto key in IndexedDB | Signs the Agent pairing proof, and signs ADR 0008 Agent challenges inside the session. |
| Brain Noise static, X25519 | Brain | Brain encrypted store (ADR 0019), one key per pairing | Noise KK responder static. |
| Agent Noise static, X25519 | Agent | Private key wrapped under a non-exportable AES-GCM key in IndexedDB | Noise KK initiator static. |

The raw Agent Noise private key exists in extension memory only while a handshake runs. Replacing any of the four keys requires a new pairing.

## Encoding

Transcripts are a domain string followed by fields. Each field is `uint32_be(length) || bytes`. Strings are UTF-8. Integers are unsigned 64-bit big-endian. Keys, nonces, and digests are raw bytes. No JSON serialization takes part.

Pairing messages encode 32-byte values as canonical unpadded base64url and signatures as 64-byte P1363 `r || s` in canonical unpadded base64url. JWKs contain exactly `kty` `EC`, `crv` `P-256`, `x`, and `y`, and must be valid curve points.

Domains, each ending in one zero byte:

| Name | Value |
| --- | --- |
| Grants | `OFCA-LOCAL-PAIRING-GRANTS-V1\0` |
| Transcript | `OFCA-LOCAL-PAIRING-TRANSCRIPT-V1\0` |
| Proof | `OFCA-LOCAL-PAIRING-PROOF-V1\0` |
| Comparison code | `OFCA-LOCAL-PAIRING-CODE-V1\0` |

## Grant digest

`grant_digest` identifies the exact grants Brain presented at pairing. For `creator_account_binding` then `installation_grant` (ASCII order), frame the grant type as a string and then the raw 32-byte SHA-256 of the exact compact JWS. `grant_digest` is SHA-256 over the grants domain followed by those four fields.

## Pairing transcript

`pairing_digest` is SHA-256 over the transcript domain followed by these fields in this order:

1. `suite`: `Noise_KK_25519_ChaChaPoly_SHA256`
2. `pairing_id`: 32 random bytes chosen by Brain
3. `generation`: integer
4. `organization_id`
5. `installation_id`
6. `installation_key_id`
7. `installation_key_jkt`
8. `creator_account_id`
9. `agent_installation_id`
10. `agent_identity_key_jkt`: RFC 7638 SHA-256 thumbprint of the Agent identity JWK
11. Agent Noise static public key, 32 bytes
12. Brain Noise static public key, 32 bytes
13. `agent_nonce`, 32 bytes
14. `brain_nonce`, 32 bytes
15. `grant_digest`, 32 bytes

Fields 4 to 8 come from the verified grants. Thumbprints are the 43-character base64url strings as they appear in the grants.

## Proofs

A proof message is the proof domain, then the role (`brain` or `agent`) as a string, then `pairing_digest`. Each proof is ES256 over the proof message: ECDSA P-256 over its SHA-256, encoded as 64-byte P1363 with low S. Signers normalize S. Verifiers reject high S.

Brain signs with the installation key through the installation-key abstraction. Agent signs with its identity key. The proof domain differs from every other installation-key signing domain, so no other installation-key signature verifies as a pairing proof.

## Comparison code

Take the first four bytes of SHA-256 over the code domain followed by `pairing_digest` as one field. Read them as `uint32_be`, reduce modulo 1,000,000, and zero-pad to six digits. Bridge and the Agent popup both display it as two groups of three digits.

## Exchange

Pairing uses the WebSocket path `/ws/agent/pairing` on the local Brain origin. Messages are UTF-8 JSON text frames with closed schemas: each is an object whose `type` member names the message, and unknown, duplicate, or missing members are rejected. A frame is at most 36,864 bytes, and each compact grant at most 16,384 characters. Any violation closes the socket with a fixed error code and no detail.

1. An authenticated Bridge operator opens a pairing window for one approved creator account. Brain allocates the next generation for the installation, a random `pairing_id`, a fresh Noise static key pair, and a fresh `brain_nonce`. The window lasts at most 300 seconds, and one installation has at most one open window.
2. Agent → Brain `pair.request`: `agent_installation_id`, `agent_identity_jwk`, `agent_noise_key`, `agent_nonce`. Agent generates a fresh Noise key pair and nonce for every attempt.
3. Brain → Agent `pair.offer`: `pairing_id`, `generation`, `creator_account_id`, `brain_noise_key`, `brain_nonce`, `installation_jwk`, `installation_grant`, `creator_account_binding`, `brain_proof`. Brain sends the offer only for the first request in an open window. A second request cancels the window and both sockets close.
4. Agent verifies the offer under "Agent checks" and stores the pending pairing. Agent → Brain `pair.confirm`: `pairing_id`, `agent_proof`. Agent displays the comparison code.
5. Brain verifies `agent_proof` against the transcript it computed. Bridge displays the comparison code, the Agent identity key thumbprint, and the creator account.
6. The operator confirms or declines in Bridge before the window expires. Brain → Agent `pair.result`: `pairing_id` and `outcome`, where `outcome` is `confirmed`, `declined`, `expired`, or `cancelled`. This message is a hint only. Agent treats a pairing as complete only after the session step below.
7. On `confirmed`, Agent opens a session using the pending pins. Brain admits a KK handshake only from the confirmed Agent Noise key. Agent commits its pin once the handshake, both fixed session confirmations, and the session authorization succeed.

Each message step has a 10-second deadline. The wait for the operator is bounded by the window. Agent needs an open popup to pair, and closing it cancels the pending pairing.

## Agent checks

Agent accepts an offer only when all of the following hold. It applies them in the order listed and refuses on the first failure; each vector case names the refusal it expects.

- The offer matches its schema.
- `brain_noise_key` is not a small-order X25519 point and differs from `agent_noise_key`.
- `brain_nonce` differs from `agent_nonce`.
- Each grant verifies under grant profile `urn:bridge-clean:grant-profile:v1` with a key from the packaged trust set whose purpose is `installation-binding`. `kid` only selects among packaged keys.
- Each grant is accepted as current or within grace at the Agent's clock, with the profile's lifetimes, grace periods, and 60-second not-before tolerance. The results match the `contracts/grant-profile-v1` vectors for both grant types.
- Each grant's `grant_type`, `aud`, `typ`, `iss`, `profile`, and `sub` have the values the profile requires for Brain audiences.
- Both grants carry the same `organization_id`, `installation_id`, `installation_key_id`, and `installation_key_jkt`.
- The RFC 7638 thumbprint of `installation_jwk` equals `installation_key_jkt`.
- `creator_account_binding.creator_account_id` equals the offer's `creator_account_id` and the account Agent detects locally.
- `generation` is higher than the highest generation Agent has admitted for that `installation_id`.
- `brain_proof` verifies under `installation_jwk` over the transcript Agent computes from its own request and the offer.

Agent has no revocation list for grants. Brain holds hosted revocation state and refuses to pair or serve when its grants are revoked.

## Brain checks

Brain accepts `pair.confirm` only when the window is open, the request was the first in that window, the Agent JWK is a valid point, the Agent Noise key is not small-order and differs from the Brain Noise key, the nonces differ, and `agent_proof` verifies under the Agent JWK. Brain also applies its ADR 0008 grant checks, including revocation state. Confirmation is a compare-and-set on the window state. If the window was cancelled or expired, or a revocation is recorded, before the commit, the confirmation fails.

## Durable state

Agent uses one IndexedDB transaction for each state change. Brain uses one `auth.sqlite3` transaction.

Agent keeps:

- at most one pending pairing: its request, the wrapped candidate Noise key, the deadline, and, once the offer verifies, `pairing_id`, `generation`, the verified identities, the Brain Noise public key, `pairing_digest`, and `grant_digest`;
- at most one pin: the same fields, `agent_installation_id`, and the wrapped Noise key;
- an epoch that forgetting the companion advances, which fences every pending commit;
- the highest admitted generation per `installation_id`, which survives forgetting a pin.

Agent refuses to start a pairing while it holds a pin. Pairing again requires forgetting the companion first.

Brain keeps:

- windows with their state and the fields received;
- pins: `pairing_id`, `generation`, `agent_installation_id`, the Agent JWK and thumbprint, the Agent Noise public key, `creator_account_id`, `pairing_digest`, the encrypted Brain Noise private key, the confirming principal, and time;
- the highest generation allocated per installation.

A confirmed pairing for the same Agent installation and account replaces the older pin in the same transaction, and the older pin's sessions close. Cancellation, consent withdrawal, a change of detected account, or revocation during verification or the handshake changes the pending version, and the commit then fails. Noise handshake state, cipher state, and transport nonces never persist.

## Session binding

Agent is the KK initiator. The prologue is the UTF-8 string `ofca-companion-session/v1;agent-to-brain;no-early-data`, then one zero byte, then the pin's `pairing_digest`.

Brain's first application record is `session.authorization`, carrying its current `installation_grant` and `creator_account_binding`. Agent applies the grant rules from "Agent checks". It also requires `organization_id`, `installation_id`, `installation_key_id`, `installation_key_jkt`, and `creator_account_id` to equal the pinned values. Agent sends no Full-mode record until this check passes. Every record inside the session carries a one-byte type discriminator under a 16-byte AEAD tag, so a routine application record holds at most 4,079 plaintext bytes inside its 4,096-byte frame, and the single `session.authorization` record holds at most 36,847 inside the 36,864-byte frame ceiling. Two grants at the character limit encode to 32,853 bytes, so the widest authorization the contract permits fits the record that carries it. A session carries exactly one `session.authorization`. Agent closes the session when a check fails, when the earliest grant's `exp` plus grace passes, or 900 seconds after it started, whichever comes first. Refreshed grants take effect in the next session.

## Revocation and recovery

Revoking a pairing in Bridge deletes Brain's pin and closes its sessions. The next KK handshake fails, and Agent reports that the companion revoked the pairing. Forgetting the companion in the Agent popup deletes Agent's pin and pending state but keeps the generation high-water mark.

Reinstalling either endpoint, or losing any key in the table above, requires a new pairing. Recovery never trusts the current loopback listener, and an old proof never authorizes a new key.

A privileged attacker who restores a whole old local profile restores both pins and high-water marks. This contract does not detect that.

## Vectors

`contracts/companion-pairing-v1/` fixes the grant digest, transcript, both proof messages, comparison code, and prologue for one pairing, and carries negative cases for every Agent and Brain check. The Agent tests read it through `extension/test-fixtures/pairing/vendored-vector.mjs` and the Python reference through `tools/companion-session-spike/local_pairing.py`; both verify every file against the contract manifest and the consumer pin before use, so the two implementations read the same bytes or fail. Implementations verify proof signatures rather than compare them, because ECDSA signers may use random nonces.

The vectors publish no private key. Each fixture key is named by a public label, and the tests derive the key from that label using the contract's test-fixture derivation, which is test-only by construction.
