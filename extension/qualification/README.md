# Extension release verification

<!-- CODE-VERIFY: Check build.mjs, signer-release.mjs, verify-release.mjs, release.spec.mjs, and acceptance-evidence.mjs before changing commands, artifact gates, browser requirements, or acceptance claims. -->

Run `npm ci`, `npm test`, and `npm run check:architecture` from `extension/`.
Install the pinned automation browser with `npx playwright install chromium`.

`build.mjs` owns the Chrome 132 target, output isolation, deterministic compilation,
read-only graph, dependency integrity and exact ZIP audits. `--outdir=<directory>`
selects a fresh candidate directory. A failed candidate is never promoted over an
existing release. Package and manifest versions must agree. Signer 0.2.0 is bound
to its reviewed archive digest and numeric release coordinates in
`signer-release.mjs`; both build audits check those identities.
See [signer integration qualification](signer-0.2.0.md) for its evidence and rule gate.

```powershell
npm run verify:release -- --packaged-signing-rule=<rule.json> --legal-release-bindings=<bindings.json> --privacy-policy-url=<https-url> --chromium-min=<chromium-132.exe> --chromium-current=<current-chromium.exe> --acceptance-evidence=<acceptance.json>
```

The runner requires a clean source revision. It runs unit tests and architecture
checks, packages into a fresh directory, audits that ZIP, extracts its allowlisted
files and runs Playwright against the extracted artifact in both supplied browsers.
It checks the ZIP digest again before promotion. Releases are retained under
`dist/release/<sha256>/` with their reports; an earlier release is preserved.
Failed candidates remain at the printed temporary path for investigation.

Automation uses persistent Chromium contexts and real popup actions. It covers
fresh install with no permissions, legal choices while analytics stays off,
absence of HTTP requests during the observed popup actions, and deletion followed by reacceptance in
the same worker. `OFCA_HEADLESS=1` selects headless Chromium. These tests do not
inject consent or replace the permission API. The network observation starts
after browser launch; startup traffic remains part of recorded production acceptance.
See [Playwright's extension setup](https://playwright.dev/docs/chrome-extensions).

Browser smoke tests alone do not qualify a release. Before promotion the runner
also requires recorded production acceptance of this exact ZIP on Chrome 132 and
the current browser major. Chrome installation must use a supported installation
path. Native permission prompts and the production companion must be exercised
without automation bypasses. The required scenario IDs are exported by
`acceptance-evidence.mjs`; each needs a `passed` result and an evidence reference.
This records human qualification, not a substitute claim that the unit-test
fixtures exercised the production platform.

The acceptance document uses schema `ofca-extension-release-acceptance/v2` and
contains `artifact_sha256`, `source_revision`, `tester`, `performed_at`,
`companion_version`, `companion_transport` (`ws://127.0.0.1:17871/ws/agent`),
`cryptographic_session` (`Noise_KK_25519_ChaChaPoly_SHA256`),
`pairing` (`verified_grants_and_comparison`), `ambient_credentials` (`absent`),
`authentication` (`verified`),
`permission_prompt` (`native`), and `bypasses_used` (`false`). Its `browsers` array
contains `{ major, installation: "supported", scenarios }` for each browser;
each scenario is `{ id, result: "passed", evidence: "<report reference>" }`.
The report hashes and retains this document alongside the ZIP.

Without acceptance evidence the runner executes the automated checks, retains the
candidate, then fails promotion. Use that candidate for the recorded tests and
rerun with the resulting evidence. Production inputs must be used for production
acceptance; synthetic legal/signing fixtures only validate implementation.

Full capture requires a validated platform-to-companion account mapping and an
authenticated Noise session with the provisioned, locally approved Brain key.
The extension fails closed while either dependency is absent. The WebSocket
upgrade carries no cookies, authorization header, or credential subprotocol.
Bridge stays at `http://bridge.localhost:17871`; its browser security is a
separate architecture boundary. See [companion transport](../../docs/companion-session-transport.md).

`companion-origin-browser.mjs` loads the packaged WASM under the production CSP
and verifies cookie isolation and refusal of a hostile listener on port 17871.
`companion-production-browser.mjs` runs the production client and Brain routes
with pinned, reconstructable test grants and their declared verifier clock. It
checks code comparison, local approval, encrypted authentication, storage access,
config retrieval, rotation, browser restart, revocation, and 512 KiB records in
both directions. These test tools
never enter the shipped artifact or replace exact-ZIP production acceptance.
Both browser gates run on Chrome 132 and current Chromium. Set `PYTHON` to the
test interpreter with `requirements-dev.txt` installed, then run from `extension/`:

```powershell
node qualification/companion-origin-browser.mjs <browser-executable> <origin-report.json>
node qualification/companion-production-browser.mjs <browser-executable> <session-report.json>
```
