# Agent companion pairing components

<!-- CODE-VERIFY: Check companion-agent-identity.mjs, companion-pairing-store.mjs, pairing-contract.mjs, companion-noise-session.mjs, the MV3 storage harness, and the shipping background/build imports before changing implementation or qualification claims. -->

The Agent pairing modules implement local identity, receipt verification, transactional pin storage, and a Snow session adapter. They are not connected to the shipping background entry or Full-mode routing. Preview and the shipping manifest/CSP remain unchanged.

## Contract input

`test-fixtures/pairing/authority-vector.json` is the unmodified interoperability fixture from approved authority source revision `ad0e46f368a7d39487cfa0df936f1a02777a9643`, path `contracts/companion-pairing-v1.interop.json`. It contains a public synthetic test key, never a production trust anchor. Tests independently reproduce enrollment, proof, grant, and receipt-binding bytes and verify the signed fixture.

The verifier accepts only the closed ES256 profile and exact pending context. Its trust loader requires a production-usable, purpose-scoped `pairing-receipt-v1` public trust set, validates key identifiers/thumbprints, and rejects templates and fixture-marked entries. No trust set is shipped by these modules.

## Local state and session ownership

`openPairingStore` retains a dedicated non-exportable P-256 identity and AES-GCM wrapping key as IndexedDB CryptoKeys. Snow generates X25519 candidate material; the store encrypts its private bytes before persistence and clears the temporary buffer. JavaScript buffer clearing does not prove complete memory erasure.

Pairing begins with independently retained local expectations and a fresh Agent nonce. Freezing the approved context checks those expectations. Receipt verification performs no writes; final admission rechecks pending identity, epoch, deadlines, generation, revocation, replay, and trust sequence in one IndexedDB transaction. Account/consent transitions must call `cancel`; authenticated revocation delivery must call `revoke`.

The store retains generation/revocation floors across cancellation and restart. Session access checks the account, trust sequence, signed offline deadline, and persisted time high-water mark. Active-session state is never persisted. Privileged rollback of the complete browser profile remains subject to the limits described in ADR 0024.

The Agent session adapter accepts only store-admitted material and a locally packaged Snow constructor. It requires encrypted confirmations, caps frames at 4 KiB, bounds handshake/confirmation to two seconds, and caps each session at the earlier of 900 seconds and the signed offline deadline. Wall-clock rollback, monotonic-clock rollback, cancellation, invalidation, malformed records, replay, or authentication failure closes the session. The conservative record cap is 1,048,576 combined application records. Frame/record budgets and the existing spike prologue prefix still require joint Brain/release review before cutover.

## Qualification

```powershell
npm test --prefix extension
npm run check:architecture --prefix extension
npm run test:browser:pairing-storage --prefix extension
# Supply the Chrome 132 executable to repeat the MV3 storage checks:
npm run test:browser:pairing-storage --prefix extension -- <chrome.exe>
```

The MV3 storage harness uses fresh profiles and synthetic authority/key fixtures. It checks non-exportable identity persistence across browser restart, wrapped private-key reconstruction, receipt-expiry independence, account/trust fences, rollback, revocation, cancellation during verification, duplicate admission, and generation replay. It does not establish hosted authentication or qualify a shipping ZIP.

The Snow feasibility workflow builds the locked Rust/WASM dependency graph and runs the session adapter against real Snow records. Key-generation tests independently derive public keys through Node/OpenSSL. The Python/Noise and MV3 feasibility tests remain separate from production acceptance.

## Integration gates

Before importing these modules into shipping composition, require:

1. Reviewed Agent-to-authority authentication, identity registration ceremony, and authenticated revocation/status interface. The proof helpers do not implement those channels or carry customer bearer sessions.
2. An approved production trust-set release input and deterministic rotation/withdrawal handling. A changed packaged trust sequence currently requires reauthorization; it does not silently upgrade persisted pins.
3. Brain pairing/persistence/session implementation and agreement on endpoint, prologue, frame budgets, session lifetime, and record limits.
4. WASM CSP approval, reproducible build/provenance/license review, and exact shipping ZIP allowlist/audit integration. The WASM binary remains a qualification artifact.
5. Migration and end-to-end review of all secret-bearing Full-mode paths listed below, followed by exact-ZIP browser and bounded live-history acceptance.

## Full-mode boundary inventory

| Existing path | Material that must move into the authenticated session |
| --- | --- |
| `transport/config-http-adapter.mjs` and its read-only counterpart | Configuration requests/responses and auth tickets |
| `transport/agent-websocket.mjs` and read-only WebSocket client | Session/reconnect tickets, captures, messages, snapshots, commands and responses |
| `transport/chrome-adapter-core.mjs` and read-only adapter | Storage bootstrap, unseal/storage keys, credential rotation |
| `transport/local-service-endpoints.mjs` and `secure-local-fetch.mjs` | Endpoint and HTTP routing gates that must not leave a plaintext alternative |

This inventory guides the cutover review; it is not evidence that those routes have migrated. External messages, URL parameters, opening headers, error paths, and late asynchronous completions also need secret-egress checks before release.
