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

The acceptance harness requires Windows and an isolated test environment. Its existing ownership checks refuse unrelated listeners and processes. Some journeys own port 17871; do not run two lanes concurrently on one development machine or point the harness at a running personal Brain. CI provides a separate Windows runner for each lane.

## Reproduce a CI lane

After the same dependency installation and production builds above:

```powershell
node tools/e2e-capture/ci/run.mjs --lane core --list --output-dir artifacts/browser-list
node tools/e2e-capture/ci/run.mjs --lane core --output-dir artifacts/browser-core
node tools/e2e-capture/ci/run.mjs --lane catchup --output-dir artifacts/browser-catchup
```

Run these commands serially on a local machine. The list command collects the full inventory without starting a journey. The checked-in registry assigns complete files to `core` or `catchup`; unknown, missing or duplicate identities fail selection. `--lane legacy` runs the complete original selection as the rollout control. CI uses the same runner, one Playwright worker, the same production builds and existing retry/deadline settings. The catch-up negative-observation window remains unchanged.

The current inventory has 16 core and two catch-up cases. ADR 0050 retires the browser provisioning wizard and its registration/resume fixtures; their removed files are not browser-lane identities. Native setup admission, original-operation recovery, configured restart and enrollment are covered by the desktop backend/frontend suites, Windows host tests and source-matched installed qualification. This browser gate does not claim to replace installed native-host or Windows Hello qualification.

Hosted comparison uses the existing Product CI workflow with both dispatch inputs `browser_qualification` and `browser_serial_control` set to true. The serial control gets its own Windows runner at the same source/run as core and catch-up; the aggregate validates all three before publishing the Legal bundle. Normal runs skip only the optional serial control. Qualification rejects retry passes without changing the configured retry behavior. Three distinct clean dispatches provide the planned comparison samples; a targeted local run or repeated attempt of one dispatch does not replace them.

To reproduce one test after preparing the builds, use ordinary Playwright targeting:

```powershell
npm test --prefix tools/e2e-capture -- tests/capture.spec.mjs
```

This narrow run is useful feedback but does not produce complete lane qualification. To add a journey, update the registry with its file and stable identity and check the unfiltered collection. New tests cannot silently inherit an optional selection. Keep whole files together so their worker and process ownership assumptions remain intact.

`npm run test:text-analysis --prefix tools/e2e-capture` runs the five text-analysis application cases against the built SPA and real Brain with isolated signed-grant, profile and typed worker fixtures. This targeted config omits the unrelated Agent build and cannot produce browser-lane evidence. The cases remain required in the ordinary core lane. They do not qualify model accuracy, hosted issuance or installed Engine delivery.

## Check reporting without running browser journeys

Install the pinned E2E and extension Node dependencies first. The safety probes use the actual pinned Playwright package with synthetic tests; they do not launch Chromium or Brain.

```powershell
npm run test:ci-tools --prefix tools/e2e-capture
npm run test:ci-sentinels --prefix tools/e2e-capture
node --test extension/tests/stable-connection-diagnostic.test.mjs extension/tests/worker-recovery-diagnostic.test.mjs
python -m pytest --override-ini=addopts= tools/e2e-capture/tests/test_session_diagnostics.py
```

The Python command needs the `pytest` and `pytest-asyncio` versions in `requirements-dev.txt`. CI invokes it explicitly because this directory is outside the backend test tree. The reporting-safety job must succeed before either hosted browser lane starts.

## What the gate covers

The test checks the complete local capture path rather than isolated modules. It verifies that:

- the built Agent completes the production companion pairing protocol with Brain;
- an authenticated operator fixture opens one comparison-recovery window, compares
  the independently delivered extension code and confirms that exact window;
- the initial passkey registration issues the session without a second ceremony;
- Preview permission and Full pairing attach to the existing platform document
  with no product-requested tab reload;
- creator-visible fixture data reaches the durable Agent outbox and canonical Brain storage;
- Brain publishes matching derived state and Bridge reads bounded message history through authenticated interfaces;
- unacknowledged Agent data survives Brain and service-worker restarts and is replayed without duplication;
- acknowledged data is not replayed again;
- service-worker recovery can occur without reloading the platform page;
- unrelated processes, listeners, profiles, and databases are not reused or terminated by the harness.

The exact sequence numbers, row counts, recovery timing, and failure assertions are defined in `tests/capture.spec.mjs`. Keep those details in the test instead of duplicating them here.

`popup-lifecycle.spec.mjs` retains its registry identities but uses persistent
pages; the shipping action has no toolbar popup. Its Options and disclosure
checks exercise the UI. Its three explicitly named companion-client checks are
supplemental port-ownership, cancellation, and result tests against a synthetic
desktop peer. The real-Brain scenarios verify signed grants, Noise authorization,
durable pin commit, storage unseal and same-document capture through the retained
comparison-recovery ports. Native automatic task dispatch and its independent
pairing receipt belong to desktop-owner tests and installed qualification. The obsolete
manual extension pairing and Settings steps are not required by those scenarios.

The consent fixtures first verify that Full intent returns to the desktop without
changing Preview. They then supply a synthetic, already admitted `browser.access`
task for that real workspace. Disclosure review, permission gestures and the
distinct Legal audit transactions still use production controllers. This task
precondition does not qualify native launcher or browser-task admission.

The harness blocks OnlyFans requests until an explicit synthetic fixture handles
them. Deliberate test-driven page reloads remain in the durable replay checks;
separate observations reject reloads requested by the extension itself.

## Privacy and teardown

The fixture uses synthetic identities and message text. Screenshots, traces, and video are disabled in the Playwright configuration.

Session failure diagnostics capture five admission comparison flags before cleanup, preserving the original exception and clock reads. They retain no raw clock values or payloads. Pause/resume failures use the same bounded state filter; control-session events remain distinct from data-session events.

CI's additional inventory and execution sidecars contain restricted identities, outcomes, retry numbers and timing/provenance fields. They do not include arbitrary test titles, assertion messages, stacks, URLs, attachments or captured page content. The original console reporting remains available in the job log. Failed-job artifacts contain only metadata independently validated by the sanitizer, or a fixed diagnostic code when validation fails. Successful complete input artifacts additionally preserve the existing console and Legal evidence bytes, bound by their hashes; the stable aggregate validates those inputs before generating the final Legal bundle.

Each run creates temporary browser and database state. Teardown closes the resources created by that run and removes its temporary directory.

For the repository-wide test matrix, see [Test changes](../../docs/testing.md).
