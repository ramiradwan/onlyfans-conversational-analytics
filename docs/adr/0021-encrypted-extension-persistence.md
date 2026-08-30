# ADR 0021: Encrypt Full-mode extension persistence with a Brain-sealed key

- Status: accepted
- Date: 2026-08-30

## Decision

Encrypt every account-scoped IndexedDB value written by Full mode with
AES-256-GCM. Derive independent encryption and deterministic-index keys with
HKDF-SHA256 from a stable per-account root supplied by the local Brain. Use a
fresh 96-bit nonce for every write, authenticate the database/store/routing
envelope as additional data, and use HMAC-SHA256 tokens instead of plaintext
personal identifiers for private primary keys and indexes. Non-sensitive
monotonic sequence fields may remain clear where IndexedDB ordering requires
them.

Brain derives the account root from the DPAPI CurrentUser-protected installation
master established by ADR 0019. The extension never persists that root. It
persists one opaque DPAPI-protected bootstrap containing the active account's
purpose-bound Agent credential. On worker or browser restart, only the exact
configured extension origin may present that bootstrap to the loopback Brain
and receive the account root and credential in a `no-store` response. After a
successful Agent handshake, rotation requires both matching config and
reconnect tickets before Brain reseals the bootstrap with the reconnect ticket.

The opaque bootstrap is the only addition to installation-global durable
extension state beyond `agent_installation_id`. It reveals neither account
identity nor credential without the same Windows user/device protection
boundary. The session partition pointer remains an account hash and never a raw
account identifier.

Full mode refuses to open an account partition when the Brain cannot unseal its
bootstrap, the key check does not match, a ciphertext or routing envelope fails
authentication, Web Crypto is unavailable, or legacy plaintext cleanup cannot
complete. The pre-release plaintext database prefix is deleted only after a
new sealed binding is verified; the Agent then obtains a clean encrypted
snapshot. There is no plaintext fallback or in-place plaintext importer.

Preview mode remains outside this boundary. Its seven-day counts-only aggregate
and consent state continue to use `chrome.storage.local` and contain no message
content or account authentication material.

## Why

- IndexedDB and `chrome.storage.local` do not themselves establish the same
  explicit device-bound protection as Brain's DPAPI-backed persistence.
- A persisted non-extractable Web Crypto key prevents script export but does
  not specify how the browser protects the underlying key bytes on disk.
- Hiding only record bodies would still expose conversation, message, account,
  and credential lookup identifiers through object-store keys and indexes.
- A Brain-sealed bootstrap resolves MV3 worker restart without persisting a raw
  encryption key or requiring a user password.
- This is a pre-release Store item, so discard-and-resnapshot removes legacy
  plaintext more safely than a complex online rewrite.

## Consequences

- Full mode requires the local Brain to be reachable whenever a fresh service
  worker process must unlock its account partition.
- Copied extension storage cannot be opened by a different Windows user/device.
  There is no portable extension-data export.
- Account-scoped database names, encrypted lookup tokens, store names, record
  counts, ciphertext sizes, and clear sequence ordering remain observable at
  rest; content and private identifiers do not.
- Encryption does not protect data from an authorized running extension, the
  local Brain after unlock, or compromise of the unlocked Windows user session.
- Re-pairing the same account re-derives the same partition root. Switching
  accounts replaces only the active sealed bootstrap; each account keeps an
  independently keyed database until local-data deletion.
- Packaging audits must verify that the Store background contains the encrypted
  adapter, sealed-bootstrap endpoints, and no plaintext credential record.

## Related

- [ADR 0008: Separate hosted provisioning from local runtime authentication](0008-production-authentication.md)
- [ADR 0010: Use bounded account-scoped history acquisition](0010-signer-history-acquisition-and-bounded-state.md)
- [ADR 0019: Encrypt local Brain persistence with device-bound keys](0019-encrypted-local-persistence.md)
- `legal/internal/decisions/SEC-001-extension-at-rest-protection.md`
