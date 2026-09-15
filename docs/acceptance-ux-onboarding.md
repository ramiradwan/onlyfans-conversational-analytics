# Chrome Store acceptance UX onboarding pass

Branch: `feat/acceptance-ux-onboarding`
Base: `main` at `01461b598aacdcd54d33bb5abc93f687225a4780`

This pass is scoped to customer acceptance from one starting instruction:

> Install the extension from the Chrome Web Store.

Preview remains independent. Full mode continues to fail closed when required desktop or production authority is unavailable.

## Before / after journey

### Before

`Install extension` → `Open popup` → Preview is usable.

Choosing Full saved authorization and opened the local desktop URL. When the desktop app was not installed or not running, the customer reached a browser connection failure with no product-owned next step. If the desktop app was obtained separately, first-run setup started with an “Installation package” field, creator approval failures collapsed into generic refusal copy, and the extension could later say Full was enabled without proving commercial analysis authority was available.

### After this branch

`Install extension` → `Open popup` → a single readiness card explains the current state and next step.

- Preview: explains that the desktop app is not required.
- Full requested, desktop runtime not reachable: explains that the desktop app is required; the extension no longer opens a dead localhost page automatically.
- Returning connected user, desktop app stopped: says the saved connection remains and offers **Retry connection**.
- Desktop app running but creator context unavailable: asks the customer to open the creator account before connection controls are shown.
- Connection required: explains that the extension and desktop app are being connected and offers **Pair device**.
- Connection in progress: keeps both views open and explains the six-digit comparison.
- Connection failed/cancelled/timed out: gives a concrete retry path.
- Full transport authenticated: says **Full mode is ready**.
- Saved connection present but delivery not authenticated: says **Full mode is temporarily unavailable** and offers recovery.

Desktop first-run setup now uses customer language:

`Connect this computer` → `Confirm your creator account` → `Approve your creator account` → `Finish desktop setup`.

Creator approval pending and hosted/offline failures now have distinct recovery messages.

The normal desktop settings screen now uses the same connection model as the extension: **Connect browser extension** → **Open connection window** → compare the six-digit code → **Confirm connection**. Technical extension identity thumbprints and raw creator account IDs are not presented to the customer.

## Implemented extension changes

- Added `extension/runtime/customer-journey.mjs` as the popup-facing readiness model.
- Added a bounded normal-runtime reachability probe over the existing local pairing WebSocket origin. It does not add extension permissions, hosted secret access, or a new authority path.
- Added a prominent status/next-step card to the popup.
- Removed the automatic localhost navigation after the customer first chooses Full.
- Preserved the existing Legal-gated Full activation path and approved Full disclosure ordering.
- Moved long Full data-handling detail behind progressive disclosure while keeping the complete disclosure available before Full is enabled.
- Added clearer connection comparison, progress, failure and returning-user recovery copy.
- Added a nonsecret `setup_incomplete` status from the existing signed-in creator-account detection signal; no Noise, authorization, cryptographic, or storage contract is changed.
- Pairing controls remain hidden until the desktop runtime is reachable and the creator context is usable, so the customer is not asked to take an action that cannot succeed.
- Added keyboard focus treatment, forced-colors support, reduced-motion handling, responsive width behavior, and a bounded scrollable Chrome popup layout.
- Kept Preview fully usable without the desktop app and avoided desktop network probes until Full has been selected.

## Implemented desktop onboarding changes

### First run

- Renamed architecture-oriented first-run steps to customer goals.
- Replaced “Installation package” in visible copy with “Setup code.”
- Stopped displaying the raw detected creator account identifier; the page confirms that the signed-in creator account was detected while retaining the identifier only for the existing backend request.
- Changed “Acquire association approval” to **Check approval** and explains that approval must be completed in secure setup first.
- Added explicit recovery copy for approval pending, hosted service unavailable, missing hosted configuration, device protection unavailable, invalid/used setup codes, and expired setup sessions.
- Clarified completion: the desktop app restarts and the customer returns to the extension.

### Normal connection screen

- Renamed the surface from “Pair extension” to **Connect browser extension**.
- Removed the customer-visible raw creator account ID.
- Removed the customer-visible extension identity thumbprint. The authenticated pairing protocol still uses its identity internally.
- Kept the six-digit comparison code as the customer confirmation signal.
- Reworded success, timeout, mismatch, cancellation, status-check failure, disconnect, and retry states in plain language.
- Renamed normal actions to **Open connection window**, **Confirm connection**, **Check connection**, **Disconnect**, and **Refresh connections** without changing their API semantics.

## Unresolved UX blockers requiring backend / release work

These are not bypassed in this branch.

1. **Desktop installer distribution is missing.** There is no published customer installer URL or GitHub release asset in the reviewed repository. The popup contains the customer action path, but the validated release configuration does not yet bind an authoritative installer URL. No URL is invented here.
2. **First-run “installed but stopped” cannot be distinguished from “not installed.”** Before the extension has a saved connection, the current browser/desktop contract exposes no trustworthy installation-presence signal. A nonsecret desktop-presence/install handoff is required if product copy must distinguish these states exactly.
3. **Hosted setup discovery is missing.** The desktop page can explain that secure setup is required, but this repository does not contain an authoritative customer onboarding URL or supported launcher handoff to it.
4. **Installation handoff profiles are incompatible across the reviewed production revisions.** Hosted onboarding emits the v1 installation package while this Brain production path requires v2. This must be fixed in the control plane/contracts integration; the desktop app must not weaken its decoder.
5. **Creator approval has no connected customer action in the reviewed hosted onboarding.** The desktop app can now represent “approval pending,” but the hosted customer surface must provide the actual approval continuation.
6. **Commercial activation is backend-only.** CapabilityLicense activation/reissue APIs exist, but the reviewed product lacks a customer-safe handoff/status contract that would let the extension or desktop UI truthfully distinguish **Activation required**, **Activation failed**, and **commercially ready** without asking for package/seat identifiers. Add a nonsecret customer readiness/activation flow rather than exposing those implementation fields.
7. **“Full mode ready” is currently local-delivery readiness, not proof of licensed analysis admission.** Until blocker 6 is resolved, this branch does not claim that first licensed analysis is customer-reachable from Chrome Store installation alone.
8. **Intermediate desktop setup is not resumable from server status.** Reload/restart reconstructs only `provisioning_ready` or `configured_restart`; registration/association/approval progress is still held in page state. Backend status must expose nonsecret resumable progress before browser/Brain restart recovery can be called production-complete.
9. **A customer-recognizable creator label is unavailable.** The provisioning/connection contracts supply an account identifier rather than a safe customer display label. This branch stops presenting the identifier as customer-facing copy, but a handle/display label contract is needed if the customer must visually distinguish multiple creator accounts.

## Tests added / updated

### Extension state and recovery

`extension/tests/customer-journey.test.mjs`

Covers:

- Preview independence.
- Desktop app required.
- Returning connected user with desktop app stopped.
- Creator-account/setup incomplete state.
- Connection required.
- Connection progress and code comparison.
- Connection failure recovery.
- Full readiness only after authenticated delivery.
- Desktop runtime probe success/failure.

`extension/tests/companion-customer-status.test.mjs`

Covers:

- Full setup reports `setup_incomplete` when no signed-in creator context is available.
- Preview returns before pairing storage or creator identity is read.

Existing Legal and Preview semantics remain compatible: Full is still selected through `LEGAL_CHOOSE_MODE_MESSAGE_TYPE`, and Preview remains the standalone mode.

### Desktop first-run

`app/provisioning/provisioning.test.mjs`

Updated to cover the customer-language state machine and adds explicit assertions for:

- creator approval pending;
- hosted/internet unavailability;
- setup-code validation/refusal recovery;
- expired setup session recovery;
- success-step progression;
- malformed backend response fail-closed behavior;
- current non-resumable reload behavior, retained as a documented blocker.

### Desktop connection screen

`frontend/tests/CompanionPairingControls.test.tsx`

Updated to cover:

- customer connection terminology;
- explicit six-digit code confirmation;
- absence of the technical identity thumbprint;
- absence of raw account IDs after account changes;
- mismatch, cancellation and timeout recovery;
- status-check retry;
- connected-extension removal scoped to the active account.

## Browser / screenshot evidence

`extension/qualification/customer-journey-visual.spec.mjs` renders the production popup HTML/CSS with deterministic customer states and captures browser screenshots for:

1. Preview ready.
2. Full requested with desktop app unavailable.
3. Creator account/setup incomplete.
4. Six-digit connection comparison.
5. Connection failed.
6. Returning connected user with desktop app stopped.
7. Full mode ready.

Additional exact-artifact states required before release:

8. Desktop first-run: setup code required.
9. Desktop first-run: creator approval pending.
10. Desktop first-run: hosted/internet unavailable.
11. Desktop first-run: setup complete / restart.
12. Normal desktop connection screen: waiting, compare, success and timeout.

The screenshot spec is authored but has not been executed as release evidence on this branch. Do not substitute fixture screenshots for exact-artifact acceptance. The UX pass changes extension bytes, so previous Store ZIP evidence is intentionally invalidated.

## New-user acceptance checklist

Starting from a clean supported machine and the Chrome Web Store:

- [x] Install extension and open popup without documentation.
- [x] Understand that Preview works without the desktop app.
- [x] Enable and use Preview independently.
- [x] Understand that Full requires the desktop app before a local connection exists.
- [ ] Install the desktop app from an in-product customer download path. **Blocked: installer URL/distribution missing.**
- [x] Understand desktop first-run steps without architecture terminology.
- [ ] Enter hosted secure setup from an in-product supported handoff. **Blocked: authoritative hosted onboarding entry missing.**
- [ ] Complete installation registration against production hosted onboarding. **Blocked: v1/v2 handoff mismatch.**
- [x] Detect and confirm the signed-in creator account without asking the user to type an internal ID.
- [ ] Complete creator approval from a connected hosted customer surface. **Blocked: hosted approval continuation missing.**
- [ ] Activate commercial Full analysis without package/seat terminology. **Blocked: customer activation/status contract missing.**
- [x] Understand what is being connected and compare only the six-digit confirmation code.
- [x] Recover from connection cancellation, mismatch, timeout and status-check failure.
- [x] Recover when a previously connected desktop app is stopped and later restarted.
- [ ] Resume intermediate first-run setup after browser/Brain restart without repeating consumed work. **Blocked: resumable backend status missing.**
- [ ] Reach and prove first licensed analysis from only “Install this extension from the Chrome Web Store.” **Blocked by the items above.**

## Validation status

- Branch base is `main` at `01461b598aacdcd54d33bb5abc93f687225a4780`; UX changes are isolated on `feat/acceptance-ux-onboarding`.
- Focused customer-journey state tests were exercised during implementation before the later normal-desktop copy pass.
- Repository-wide extension, provisioning, frontend and browser suites are qualified through draft PR #36 before new P0 UX changes proceed.
- No exact-artifact Chrome Store candidate has been built or frozen from this branch.

## Release position

Do not freeze or submit a new Store ZIP from this branch yet. The branch is an acceptance UX candidate, not a production-complete release candidate, until the P0 dependencies above are resolved and exact-artifact browser evidence passes.
