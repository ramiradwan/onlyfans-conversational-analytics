# Agent companion pairing

<!-- CODE-VERIFY: Check background-read-only.js, background.js, runtime/companion-client.mjs, runtime/companion-pairing-store.mjs, runtime/packaged-snow.mjs, transport/companion-noise-session.mjs, build.mjs, manifest.json, the companion browser drivers, and .github/workflows/native-snow-brain-feasibility.yml before changing shipping or qualification claims. -->

The shipping read-only background connects Full mode through `createCompanionClient`. Agent verifies Brain's installation proof and grants. The operator compares Agent's pairing code with Bridge before approving the peer key.

Full-mode configuration, credentials, storage-key operations and protocol traffic use the authenticated Noise session. Preview requires no companion connection.

The [pairing contract](../../docs/companion-pairing-contract.md) defines the identities and approval rules. The [session transport contract](../../docs/companion-session-transport.md) defines encrypted operations, framing, deadlines and lifecycle checks.

## Shipping composition

`background-read-only.js` supplies the companion client's storage, configuration and WebSocket adapters to the read-only runtime. `background.js` uses the same client for the authoring runtime. The popup opens a persistent comparison window; Bridge provides the authenticated confirmation and revocation controls.

The build packages the production grant trust set, static Snow glue and WASM. Directory and ZIP audits check their identities and the exact manifest policy. The CSP permits locally packaged WASM and `ws://127.0.0.1:17871`; the manifest grants no local host permission. Brain rejects credentials on socket opening and serves no Bridge HTTP content on that origin. Bridge remains at `http://bridge.localhost:17871`.

## Local state

`openPairingStore` retains a dedicated non-exportable P-256 identity and AES-GCM wrapping key as IndexedDB CryptoKeys. Snow generates the candidate X25519 key pair; the store wraps private bytes before persistence and clears the temporary buffer. JavaScript buffer clearing does not prove complete memory erasure.

Offer verification does not admit a pin. The store commits the pending pin only after the first authenticated session verifies Brain's authorization, rechecking the attempt, epoch, deadline and generation. Cancellation, forgetting and authority changes invalidate pending work and active sessions. Forgetting preserves the generation high-water mark. The store refuses an unsupported stored format without changing it.

## Qualification

Run from the repository root:

```powershell
npm test --prefix extension
npm run check:architecture --prefix extension
npm run test:browser:pairing-storage --prefix extension
# Supply the Chrome 132 executable to repeat the MV3 storage checks:
npm run test:browser:pairing-storage --prefix extension -- <chrome.exe>
```

The storage harness verifies identity persistence, wrapped-key reconstruction, account binding, cancellation, generation checks and transactional pin admission with synthetic grants. Its fixture trust set cannot be loaded as production trust.

The production browser drivers exercise the shipping client modules, production Brain routes and native Snow factory with explicit fixture grants, clock, installation-key provider and Bridge authorization. They cover comparison, encrypted authentication, storage access, configuration, rotation, browser restart, revocation and maximum-size records. Separate origin and popup drivers check hostile port ownership, ambient credential isolation and the comparison window lifecycle. See the [driver setup](../../tools/companion-session-qualification/README.md).

The [recorded qualification](noise-dependency-review.md#evidence) passed these browser gates on Chrome 132 and current Chromium. That evidence identifies the exact source revisions and archive digests.

These harnesses qualify production module integration with test inputs. The [release gate](README.md) separately requires the exact candidate ZIP, real provisioning, supported browser installation, native permission interactions and recorded production acceptance. Scoped live-history qualification is also required; fixture results do not establish it.
