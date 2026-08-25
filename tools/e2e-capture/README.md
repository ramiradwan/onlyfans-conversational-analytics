<!-- CODE-VERIFY: Verify prerequisites, package scripts, browser behavior, test coverage, temporary resources, and teardown against the E2E harness before editing. -->

# Capture E2E harness

The capture E2E harness tests the built Agent, Brain, and Bridge together in Chromium. It uses a synthetic platform fixture instead of a live account.

## Run locally

From the repository root:

```powershell
./.venv/Scripts/python -m pip install -r requirements-dev.txt
npm ci --prefix frontend
npm run build --prefix frontend
npm ci --prefix extension
npm run build --prefix extension
npm run audit --prefix extension
npm ci --prefix tools/e2e-capture
npm run install:browser --prefix tools/e2e-capture
npm test --prefix tools/e2e-capture
```

Set `OFCA_E2E_PYTHON` when the harness should use a Python interpreter other than the repository virtual environment or `python` on `PATH`.

## What the gate covers

The test checks the complete local capture path rather than isolated modules. It verifies that:

- the built Agent can pair with Brain through Bridge;
- creator-visible fixture data reaches the durable Agent outbox and canonical Brain storage;
- Brain publishes matching derived state and Bridge reads bounded message history through authenticated interfaces;
- unacknowledged Agent data survives Brain and service-worker restarts and is replayed without duplication;
- acknowledged data is not replayed again;
- service-worker recovery can occur without reloading the platform page;
- unrelated processes, listeners, profiles, and databases are not reused or terminated by the harness.

The exact sequence numbers, row counts, recovery timing, and failure assertions are defined in `tests/capture.spec.mjs`. Keep those details in the test instead of duplicating them here.

## Privacy and teardown

The fixture uses synthetic identities and message text. Screenshots, traces, and video are disabled in the Playwright configuration.

Each run creates temporary browser and database state. Teardown closes the resources created by that run and removes its temporary directory.

For the repository-wide test matrix, see [Test changes](../../docs/testing.md).
