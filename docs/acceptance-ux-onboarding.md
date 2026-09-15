# Chrome Store acceptance UX onboarding pass

Branch: `feat/acceptance-ux-onboarding`
Base: `main` at `01461b598aacdcd54d33bb5abc93f687225a4780`

This pass is scoped to customer acceptance from one starting instruction:

> Install the extension from the Chrome Web Store.

Preview remains independent. Full mode continues to fail closed when required desktop or production authority is unavailable.

## Before / after journey

### Before

`Install extension` → `Open popup` → Preview is usable.

Choosing Full saved authorization and opened the local desktop URL. When the desktop app was not installed or not running, the customer reached a browser connection failure with no product-owned next step. If the desktop app was obtained separately, first-run setup started with an “Installation package” field, creator approval failures collapsed into generic refusal copy, and the extension could later present authenticated local delivery as if paid Full analytics were ready.

### Current branch

`Install extension` → `Open popup` → one readiness card explains the current state and next step.

- Preview: the desktop app is not required and no desktop probe is made.
- Full requested, desktop runtime not reachable: the desktop app is required; the extension does not open a dead localhost page automatically.
- Returning connected user, desktop app stopped: the saved connection remains and **Retry connection** is offered.
- Desktop app running but creator context unavailable: the customer is asked to open the creator account before connection controls appear.
- Connection required: the extension and desktop app are connected through the existing authenticated pairing flow.
- Connection in progress: both views explain the six-digit comparison.
- Connection failed/cancelled/timed out: the UI gives a concrete retry path.
- Authenticated local delivery: the popup says **Desktop connected**. It does not call this paid Full readiness.
- Commercial authority missing: the popup says **Activate Full analysis**.
- Commercial authority cannot be confirmed: the popup says **Full activation needs attention** and offers recovery.
- **Full mode is ready** only when the desktop connection is authenticated, current commercial authority is active, and current licensed-analysis admission is admitted.

Desktop first-run setup uses customer language:

`Connect this computer` → `Confirm your creator account` → `Approve your creator account` → `Finish desktop setup`.

The normal desktop settings screen uses the same connection model as the extension: **Connect browser extension** → **Open connection window** → compare the six-digit code → **Confirm connection**. Technical extension identity thumbprints and raw creator account IDs are not presented to the customer.

## Implemented extension changes

- Added `extension/runtime/customer-journey.mjs` as the popup-facing readiness model.
- Added a bounded desktop-runtime reachability probe over the existing local pairing WebSocket origin. It adds no extension permission or authority path.
- Added a prominent status/next-step card to the popup.
- Removed automatic localhost navigation after the customer first chooses Full.
- Preserved the existing Legal-gated Full activation selection and approved Full data-handling disclosure.
- Added clearer connection comparison, progress, failure and returning-user recovery copy.
- Added a nonsecret `setup_incomplete` status from the existing signed-in creator-account detection signal.
- Pairing controls remain hidden until the desktop runtime is reachable and creator context is usable.
- Kept Preview fully usable without the desktop app and avoided desktop network probes until Full has been selected.
- Added a customer-safe licensed-readiness query over the already-authenticated companion channel. The extension receives only the closed `ofca-analysis-readiness/v1` enums; it receives no CapabilityLicense bytes, package, grant, license, issuance, seat or reference identifiers.
- Added separate visible rows for **Secure connection**, **Full activation**, and **Licensed analysis**.
- Removed transport-authenticated `Full mode is ready` semantics. Transport authentication now means only **Desktop connected**.

## Licensed-readiness authority

Brain evaluates customer readiness from the same current identity/account and CapabilityLicense predicates used by licensed analysis admission.

`app/security/analysis_authorization.py` exposes a read-only status evaluation:

- `commercial_authority = required | active | unavailable`
- `analysis_admission = blocked | admitted`

The evaluation does not cache or create an analysis admission. Actual processing still requires the existing `build_current_analysis_policy` / `require_cached_analysis_run` path.

`app/api/endpoints/companion_session.py` exposes that status only as encrypted `agent.analysis.readiness` after Agent authentication and matching creator-account authority. There is no new plaintext/HTTP credential or readiness authority.

## Implemented desktop onboarding changes

### First run

- Renamed architecture-oriented first-run steps to customer goals.
- Replaced “Installation package” in visible copy with “Setup code.”
- Stopped displaying the raw detected creator account identifier while retaining it only for the existing protected backend request.
- Changed “Acquire association approval” to **Check approval** and explains that approval must be completed in secure setup first.
- Added explicit recovery copy for approval pending, hosted service unavailable, missing hosted configuration, device protection unavailable, invalid/used setup codes, and expired setup sessions.
- Clarified completion: the desktop app restarts and the customer returns to the extension.

### Normal connection screen

- Renamed the surface from “Pair extension” to **Connect browser extension**.
- Removed the customer-visible raw creator account ID and extension identity thumbprint.
- Kept the six-digit comparison code as the customer confirmation signal.
- Reworded success, timeout, mismatch, cancellation, status-check failure, disconnect, and retry states in plain language.
- Renamed normal actions without changing their API semantics.

## Unresolved P0 dependencies

These are not bypassed on this branch.

1. **Desktop installer distribution is missing.** No authoritative customer installer/download URL is bound into the Store candidate yet.
2. **Hosted setup discovery is missing.** The product repository does not contain an authoritative customer entry/handoff into hosted secure onboarding.
3. **Installation handoff profile compatibility still requires cross-repository closure.** The product must consume the same production profile that hosted onboarding emits; Brain must not weaken its decoder.
4. **Creator approval continuation is still hosted-plane work.** Brain can represent approval pending, but the customer needs the actual hosted continuation.
5. **Commercial activation continuation is still missing.** The branch can now truthfully detect `Activation required`, `Activation unavailable`, and `licensed ready`, but there is not yet an in-product customer handoff that completes CapabilityLicense activation/reissue without internal package/seat terminology.
6. **Intermediate desktop setup is not fully resumable.** Server status still needs nonsecret intermediate progress across browser/Brain restart.
7. **A customer-recognizable creator label is unavailable.** The current contract provides an internal account identifier, which the UI intentionally does not expose.
8. **Installed-but-stopped versus not-installed is not trustworthy before a prior connection.** Current wording/retry is used instead of guessing installation presence.

## Tests added / updated

### Licensed readiness

`tests/test_analysis_authorization.py`

- activation required without a compatible license;
- active/admitted only with current compatible commercial authority;
- ambiguous authority fails to unavailable/blocked;
- checking readiness does not create cached analysis admission.

`tests/test_companion_analysis_readiness_rpc.py`

- readiness requires fresh Agent authentication;
- response contains only closed nonsecret state;
- cross-account identity is refused.

`extension/tests/analysis-readiness-client.test.mjs`

- readiness travels only after the authenticated companion handshake;
- malformed, extended, or impossible ready responses fail closed.

`extension/tests/customer-journey.test.mjs`

- authenticated transport alone is never Full-ready;
- activation-required and activation-unavailable are distinct;
- active commercial authority alone does not imply analysis admission;
- Full-ready requires authenticated delivery + active commercial authority + admitted licensed analysis.

### Existing journey coverage

`extension/tests/companion-customer-status.test.mjs` covers setup-incomplete and Preview independence.

`app/provisioning/provisioning.test.mjs` covers first-run customer-language progression, approval pending, hosted/offline recovery, malformed response fail-closed behavior, and the currently documented resume limitation.

`frontend/tests/CompanionPairingControls.test.tsx` covers customer connection terminology, explicit six-digit confirmation, identity-data non-disclosure, mismatch/cancel/timeout recovery, and status retry.

The Windows browser E2E assertions have been aligned to the reviewed customer copy while retaining their underlying runtime identity, provisioning and authorization assertions.

## Browser / screenshot evidence

`extension/qualification/customer-journey-visual.spec.mjs` renders the production popup HTML/CSS with deterministic states for:

1. Preview ready.
2. Desktop app required.
3. Creator account/setup incomplete.
4. Six-digit connection comparison.
5. Connection failed.
6. Previously connected desktop app stopped.
7. Full activation required.
8. Full activation unavailable.
9. Full mode ready with licensed analysis admitted.

Do not substitute fixture screenshots for final exact-artifact acceptance. The UX pass changes extension bytes, so previous Store ZIP evidence is intentionally invalidated.

## New-user acceptance checklist

Starting from a clean supported machine and the Chrome Web Store:

- [x] Open the extension and understand Preview without documentation.
- [x] Use Preview independently of the desktop app.
- [x] Understand that Full requires the desktop app.
- [ ] Install the desktop app from an authoritative in-product customer download path. **Blocked: distribution URL.**
- [x] Understand desktop first-run steps without architecture terminology.
- [ ] Enter hosted secure setup from an authoritative in-product handoff. **Blocked: hosted onboarding entry.**
- [ ] Complete production installation registration with cross-repository profile compatibility. **Blocked outside this repo.**
- [x] Detect/confirm the signed-in creator account without asking the customer to type an internal ID.
- [ ] Complete creator approval from the connected hosted customer surface. **Blocked: hosted continuation.**
- [x] Distinguish secure desktop connection from commercial Full activation and licensed-analysis admission.
- [ ] Complete commercial activation without package/seat terminology. **Blocked: hosted activation continuation.**
- [x] Show **Full mode is ready** only when current commercial authority and licensed analysis admission are both confirmed.
- [x] Recover from connection cancellation, mismatch, timeout, stopped desktop app, and readiness-check failure.
- [ ] Resume intermediate first-run setup after browser/Brain restart without repeating consumed work. **Blocked: resumable provisioning status.**
- [ ] Prove first licensed analysis from only “Install this extension from the Chrome Web Store.” **Blocked by the remaining external P0s.**

## Validation status

- Branch is based on `main` at `01461b598aacdcd54d33bb5abc93f687225a4780` and remains isolated on `feat/acceptance-ux-onboarding`.
- Draft PR #36 is the qualification path; it remains Draft and must not be merged yet.
- No signed integration commit or final Store package has been created.

## Release position

Do not freeze or submit a new Store ZIP from this branch yet. Truthful paid-readiness semantics are implemented, but the remaining distribution/hosted/resume P0s must be closed and exact-artifact acceptance must pass first.
