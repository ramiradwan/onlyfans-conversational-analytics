# Authenticated companion pairing contract proposal

This proposal supplies the trust bootstrap for [ADR 0024](adr/0024-authenticated-companion-sessions.md). It is implemented as an isolated signed-receipt verifier and admission model, not as a production hosted service or a change to vendored contracts.

## Authority and delivery

Agent and Brain independently pin a dedicated pairing-receipt issuer verification key through signed software or a transition authenticated by an existing trust anchor. The receipt cannot supply its own trusted key or key URL. Existing installation-binding signing keys must not acquire this new purpose without contract and key-separation review.

The customer selects the intended installation and account through the authenticated hosted provisioning UI. Agent creates a fresh 256-bit challenge and a local Noise key pair. Brain creates its own fresh 256-bit challenge and Noise key pair. The endpoints retain their pending enrollment context, including their own public key, selected identities, consent/pairing generation, and deadline. Network-provided values must not overwrite these local expectations.

Each endpoint submits its public enrollment material over the authenticated hosted provisioning channel. Brain proves possession of the registered TPM installation signing key over an issuer challenge and the exact enrollment digest, including its Noise public key. Agent proves possession of its enrolled Agent identity key over its challenge and Noise public key. These signing proofs authorize delegated key-agreement keys; possession of a Noise key alone is not customer approval.

The hosted issuer verifies both proofs, their one-time challenges, current installation/account grants, and explicit customer approval of that exact installation/Agent/account tuple. It atomically consumes the enrollment request and signs one receipt binding both endpoints. A request that changes either key or identity needs new approval and challenges. The issuer never receives either Noise private key or any conversation data.

Both endpoints retrieve the receipt through their authenticated hosted provisioning channels. Local browser messages may notify that enrollment is ready, but they carry neither secrets nor authority to install a pin. No receipt, account identifier, auth ticket, or storage bootstrap credential is sent over the unauthenticated loopback transport. The sole pre-session WebSocket exchange is the bounded Noise handshake and encrypted confirmation.

## Signed artifact

The spike uses compact ES256 JWS with the exact header fields `alg`, `kid`, and `typ`; `typ` is `ofca-companion-pairing+jwt`. It rejects embedded keys, remote key references, duplicate JSON members, other algorithms, and other token types. It uses [PyJWT](https://pyjwt.readthedocs.io/en/stable/) for signature verification and follows the purpose separation described in [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725.html).

| Claim | Meaning |
| --- | --- |
| `iss`, `aud` | Pinned issuer and exact audience `urn:ofca:companion-pairing:v1` |
| `iat`, `exp`, `jti` | Issuance, bounded enrollment expiry, and random 256-bit receipt identifier |
| `installation_id`, `agent_id`, `account_id` | Exact approved endpoint/account tuple |
| `agent_key`, `brain_key` | Canonical unpadded base64url encodings of the two 32-byte X25519 public keys |
| `agent_nonce`, `brain_nonce` | Independent 256-bit challenges, encoded as lowercase hex |
| `generation` | Monotonically advancing pairing generation |
| `grant_digest` | Digest identifying the exact validated authorization context |
| `suite` | Exact `Noise_KK_25519_ChaChaPoly_SHA256` profile |

The prototype limits enrollment receipts to five minutes and 8 KiB. No extra claims are accepted. The deployment issuer URL, production key identifiers, authorization-digest definition, generation authority, and actual signed proof profiles must be assigned by the cross-plane contract owner; the prototype does not invent production coordinates.

## Endpoint admission

Each endpoint verifies the issuer signature and purpose, then checks the selected installation, Agent, account, both challenges, generation, grant digest, suite, and time against its pending authenticated context. It checks that its own signed Noise public key matches the locally held private key. The peer key is accepted only as a consequence of these checks.

Admission atomically consumes the pending enrollment and stores the peer pin with its authorization metadata. Cancellation, consent/account changes, or revocation must fence the commit, including if they occur during signature verification. Duplicate receipt delivery must not replace a pin or revive a cancelled request. Durable storage must preserve consumed challenges and the highest authorized generation across restart; rollback-resistant handling is a production review item.

Both endpoints hash the same verified claim set into the Noise prologue binding. The spike specifies sorted compact ASCII-escaped JSON with string claims and safe integer numeric claims, then SHA-256. Browser interoperability must test this exact encoding. The JWS signature bytes are excluded from the binding so signature encoding variants cannot split an otherwise identical enrollment context.

After pin admission, a new Noise handshake proves live possession of both authorized keys. Neither signed authorization alone nor a health response enables Full mode. Noise completion is followed by encrypted confirmations before any application operation is accepted. Production grants, consent epochs, and operation authorization remain mandatory after cryptographic authentication.

## Lifetime and replacement

The five-minute receipt expiry bounds enrollment, not the intended offline lifetime of a production pairing. The spike conservatively also refuses new sessions after receipt expiry; it does not implement production offline authorization. Production pin lifetime must be derived from the existing grant/revocation contract, without introducing an indefinite grant or requiring a hosted round trip for every local session.

Changed keys require a newly approved, higher-generation receipt and fresh challenges. Known revocations immediately close active sessions and prevent reconnect. Production must define how signed revocation updates and offline grace interact with active-session deadlines. Lost private keys cannot be recovered by trusting the process now listening on port 17871.

The in-memory gate models atomic admission and cancellation. It is not a persistence implementation, a TPM adapter, a production issuer, or a browser verifier. The cryptographic-session and trust-bootstrap designs require review before any production code changes.

## Cross-plane handoff

The hosted provisioning and contract maintainers must define and implement the dedicated receipt purpose, issuer key distribution, both endpoint proof profiles, customer approval, one-time challenge consumption, authorization digest, generation/revocation rules, and protected receipt retrieval. Agent then needs an independent browser verifier and durable pin store; Brain needs the corresponding issuer client and Noise-key protection. Existing grant schemas and production trust sets remain unchanged until that contract is approved and published.
