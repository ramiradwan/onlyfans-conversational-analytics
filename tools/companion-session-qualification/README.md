# Production companion browser qualification

`serve.py` hosts the production pairing and session routes in an isolated process
with real SQLCipher repositories and the production native Snow factory. It uses
the reconstructable, pinned contract fixture grants and their declared verifier
clock. The fixture installation-key provider and authenticated Bridge policy are
test inputs; production trust and expiry checks remain unchanged.

The driver is `extension/qualification/companion-production-browser.mjs`. It
starts this process with hidden windows and drives public pairing controls over
stdin. The qualification server and worker are excluded from Brain and Agent
packages. Their injected provider, fixture clock, and test trust set are not used
by the production composition. Reports contain only artifact hashes and pass/fail
fields. Test exception payloads and application diagnostics are not echoed.

The browser loads the production client, durable pairing store, grant verifier,
and packaged Snow WASM under the production manifest's permissions and CSP. The
temporary worker adds test entry points and explicitly selects the fixture trust
set. It compares the two pairing codes before local confirmation, qualifies
encrypted authentication, storage unsealing, config and rotation, restarts the
browser profile, and verifies revocation. An exact 512 KiB snapshot is committed
to canonical storage; a 512 KiB inbound protocol record exercises the bounded
browser fragment queue. The inbound command fixture is observed, never executed.

Run through the driver with `PYTHON` pointing to an interpreter that has
`requirements-dev.txt` installed. Port 17871 must be free. CI runs Chrome 132 and
current Chromium and retains the report alongside the hostile-listener,
independent-oracle, and frozen-runtime evidence.

This qualifies production module integration. Release acceptance still requires
the exact candidate ZIP, real provisioning, native permission interactions, and
the supported browser installation path.
