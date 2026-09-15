<!-- CODE-VERIFY: app/api/endpoints/companion_session.py app/security/companion_session_authority.py app/transport/companion_records.py app/transport/companion_channel.py extension/transport/companion-channel.mjs extension/transport/companion-fragments.mjs extension/runtime/companion-client.mjs -->

# Companion session transport

Full-mode communication uses `Noise_KK_25519_ChaChaPoly_SHA256` on `ws://127.0.0.1:17871/ws/agent`. [ADR 0024](adr/0024-authenticated-companion-sessions.md) defines the trust boundary; the [pairing contract](companion-pairing-contract.md) defines the pinned identities and fixed prologue. Preview opens no companion connection.

## Admission

The first binary message contains the raw 32-byte pairing ID followed by the initiator's first Noise handshake message. The selector is public. No credential is sent in a URL, opening header, subprotocol, or handshake payload.

Brain sends the responder handshake. Agent sends the encrypted bytes `00 || UTF8("client-ready")`; Brain replies with `00 || UTF8("server-ready")`. Brain then sends one encrypted `01 || UTF8(JSON)` record containing `session.authorization` and its two exact compact grants. Agent verifies that authorization against its pins before sending any Full-mode record.

Brain admits the complete handshake within two seconds. The authorization ciphertext is at most 36,864 bytes. Routine ciphertexts are at most 4,096 bytes. Sessions expire within 900 seconds, at the applicable grant boundary, or after 1,048,576 records, whichever occurs first. Reconnects use new handshakes and fresh challenges. Invalid framing, authentication, replay, clock rollback, authorization loss or cancellation closes the session with a fixed error.

## Routine documents

Each encrypted record contains `01 || UTF8(JSON fragment)`. A fragment has exactly these fields:

```json
{"type":"fragment","id":"canonical-uuid","index":0,"final":true,"data":"base64url"}
```

`data` is canonical unpadded base64url encoding of at most 2,800 raw bytes. Every nonfinal chunk has exactly 2,800 bytes. Indexes start at zero and increase contiguously under one UUID; interleaved documents are refused. One direction assembles at most one document, up to 524,288 UTF-8 bytes, within ten seconds. Duplicate JSON keys, nonfinite values and malformed UTF-8 are refused. A complete outgoing document also has a ten-second deadline. Incoming and outgoing application queues and concurrent RPCs are bounded.

The reconstructed document is either an existing protocol v2 envelope or an RPC. Protocol schemas, canonical-ingestion ownership, fencing tokens and acknowledgement rules remain unchanged.

```json
{"type":"rpc.request","id":"canonical-uuid","method":"agent.challenge","params":{}}
```

Responses contain exactly `type`, the same `id`, and either `result` or a fixed payload-free `error`. Repeated RPC IDs and more than 1,024 requests in one session are refused.

## Encrypted operations

| Method | Purpose |
| --- | --- |
| `agent.challenge` | Issue a fresh session-bound challenge with a 30-second deadline. |
| `agent.authenticate` | Verify the pinned Agent P-256 identity proof; issue an Agent ticket and opaque storage bootstrap. |
| `agent.config.get` | Authenticate the current Agent configuration ticket; return the immutable configuration or an ETag match. |
| `agent.storage.unseal` | Validate the account-bound bootstrap and return the stable account storage key with this session's fresh ticket. |
| `agent.storage.rotate` | Validate this session's reconnect and configuration credentials and reseal its bootstrap. |

After authentication and storage unlock, Agent sends the unchanged protocol v2 `agent.hello` inside the encrypted stream. Tickets cannot authorize another session or a plaintext route. The configuration credential becomes reusable only within the handle that consumed it, with authorization rechecked on every use. Agent retains session credentials and unlocked keys in memory; durable Full data remains encrypted.

The identity proof uses canonical base64url low-S ES256 P1363. Its signing input is `UTF8("OFCA-AGENT-REQUEST-V1") || 00`, followed by uint32 big-endian byte-length-prefixed UTF-8 fields, in order: session ID, challenge, `POST`, `/agent/session-ticket`, lowercase SHA-256 hexadecimal digest of the empty body, `agent-websocket`, Agent installation ID, creator account ID, base64url pairing ID, and Brain installation ID. The method and path are purpose bindings, not HTTP endpoints. `request_signing_message()` in `app/security/companion_session_authority.py` owns this encoding.

## Lifecycle

Every canonical write rechecks the session's authority while holding an authentication transaction through the synchronous data commit. Revocation serializes against that check. Brain also polls idle session authority every 250 milliseconds. Grant refresh changes the references selected for the next session; references already frozen into a running session cannot be silently substituted.

Bridge can revoke an account-scoped pin from Settings. Agent can forget its companion from the popup. Pairing comparison opens in a persistent extension window so switching focus to Bridge does not cancel the attempt; closing that window cancels it. Forgetting or account/consent changes abort pending work, close active sessions and prevent late results from restoring authority. A cancelled or expired pairing cannot admit a later signer or storage completion. Highest admitted pairing generations survive forgetting.

Production HTTP routes do not release Agent tickets, configuration or storage keys. The Agent socket origin serves no Bridge HTTP content and issues no cookies. The extension CSP permits the exact loopback WebSocket origin, with no local host permission. The public provisioning-identity external message is available only before pairing and carries no Full-mode credential.
