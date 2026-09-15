# Chrome Store acceptance UX onboarding pass

Branch: `feat/acceptance-ux-onboarding`  
Base: `feat/capability-license-runtime` at `3bd975d5a3242c26ec9b86eb58cfe9f795987176`

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
- Returning paired user, desktop app stopped: says the saved connection remains and offers **Retry connection**.
- Desktop app running but creator context unavailable: asks the customer to open the creator account before pairing.
- Pairing required: explains that the extension and desktop app are being connected and offers **Pair device**.
- Pairing in progress: keeps both views open and explains the six-digit comparison.
- Pairing failed/cancelled/timed out: gives a concrete retry path.
- Full transport authenticated: says **Full mode is ready**.
- Saved connection present but delivery not authenticated: says **Full mode is temporarily unavailable** and offers recovery.

Desktop first-run setup now uses customer language:

`Connect this computer` → `Confirm your creator account` → `Approve your creator account` → `Finish desktop setup`.

Creator approval pending and hosted/offline failures now have distinct recovery messages.

## Implemented extension changes

- Added `extension/runtime/customer-journey.mjs` as the popup-facing readiness model.
- Added a bounded normal-runtime reachability probe over the existing authenticated pairing WebSocket origin. It does not add extension permissions, hosted secret access, or a new authority path.
- Added a prominent status/next-step card to the popup.
- Removed the automatic localhost navigation after the customer first chooses Full.
- Preserved the existing Legal-gated Full activation path and approved Full disclosure ordering.
- Moved long Full data-handling detail behind progressive disclosure while keeping the complete disclosure available before Full is enabled.
- Added clearer pairing comparison, progress, failure and returning-user recovery copy.
- Added keyboard focus treatment, forced-colors support, reduced-motion handling, responsive width behavior, and a bounded scrollable Chrome popup layout.
- Kept Preview fully usable without the desktop app.

## Implemented desktop onboarding changes

- Renamed architecture-oriented first-run steps to customer goals.
- Replaced “Installation package” in visible copy with “Setup code.”
- Stopped displaying the raw detected creator account identifier; the page confirms that the signed-in creator account was detected while retaining the identifier only for the existing backend request.
- Changed “Acquire association approval” to **Check approval** and explains that approval must be completed in secure setup first.
- Added explicit recovery copy for approval pending, hosted service unavailable, missing hosted configuration, device protection unavailable, invalid/used setup codes, and expired setup sessions.
- Clarified completion: the desktop app restarts and the customer returns to the extension.

## Unresolved UX blockers requiring backend / release work

These are not bypassed in this branch.

1. **Desktop installer distribution is missing.** There is no published customer installer URL or GitHub release asset in the reviewed repository. The popup is prepared to render an install action only when a real HTTPS download URL is provided, but no URL is invented here.
2. **First-run “installed but stopped” cannot be distinguished from “not installed.”** Before the extension has a saved pairing, the current browser/desktop contract exposes no trustworthy installation-presence signal. A nonsecret desktop-presence/install handoff is required if product copy must distinguish these states exactly.
3. **Hosted setup discovery is missing.** The desktop page can explain that secure setup is required, but this repository does not contain an authoritative customer onboarding URL or supported launcher handoff to it.
4. **Installation handoff profiles are incompatible across the reviewed production revisions.** Hosted onboarding emits the v1 installation package while this Brain production path requires v2. This must be fixed in the control plane/contracts integration; the desktop app must not weaken its decoder.
5. **Creator approval has no connected customer action in the reviewed hosted onboarding.** The desktop app can now represent “approval pending,” but the hosted customer surface must provide the actual approval continuation.
6. **Commercial activation is backend-only.** CapabilityLicense activation/reissue APIs exist, but the reviewed product lacks a customer-safe handoff/status contract that would let the extension or desktop UI truthfully distinguish **Activation required**, **Activation failed**, and **commercially ready** without asking for package/seat identifiers. Add a nonsecret customer readiness/activation flow rather than exposing those implementation fields.
7. **“Full mode ready” is currently local-delivery readiness, not proof of licensed analysis admission.** Until blocker 6 is resolved, this branch does not claim that first licensed analysis is customer-reachable from Chrome Store installation alone.
8. **Intermediate desktop setup is not resumable from server status.** Reload/restart reconstructs only `provisioning_ready` or `configured_restart`; registration/association/approval progress is still held in page state. Backend status must expose nonsecret resumable progress before browser/Brain restart recovery can be called production-complete.
9. **A customer-recognizable creator label is unavailable.** The provisioning extension response supplies only an account identifier. This branch stops presenting that identifier as customer-facing copy, but a handle/display label contract is needed if the customer must visually distinguish multiple signed-in creator accounts.

## Tests added / updated

### Extension

`extension/tests/customer-journey.test.mjs`

Covers:

- Preview independence.
- Desktop app required.
- Returning paired user with desktop app stopped.
- Creator-account/setup incomplete state.
- Pairing required.
- Pairing progress and code comparison.
- Pairing failure recovery.
- Full readiness only after authenticated delivery.
- Desktop runtime probe success/failure.

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

## Browser / screenshot evidence

Major visual states to capture from the built candidate:

1. Preview available / Preview ready.
2. Full requested with desktop app unavailable.
3. Creator account/setup incomplete.
4. Pairing required.
5. Pairing comparison code.
6. Pairing failed.
7. Returning paired user with desktop app stopped.
8. Full mode ready.
9. Desktop first-run: setup code required.
10. Desktop first-run: creator approval pending.
11. Desktop first-run: hosted/internet unavailable.
12. Desktop first-run: setup complete / restart.

Do not use these screenshots as release evidence until they come from the exact final built extension/desktop artifacts. The UX pass changes extension bytes, so previous Store ZIP evidence is intentionally invalidated.

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
- [x] Understand and recover from pairing progress, cancellation/failure, and comparison-code confirmation.
- [x] Recover when a previously paired desktop app is stopped and later restarted.
- [ ] Resume intermediate first-run setup after browser/Brain restart without repeating consumed work. **Blocked: resumable backend status missing.**
- [ ] Reach and prove first licensed analysis from only “Install this extension from the Chrome Web Store.” **Blocked by the items above.**

## Release position

Do not freeze or submit a new Store ZIP from this branch yet. The branch is an acceptance UX candidate, not a production-complete release candidate, until the P0 dependencies above are resolved and exact-artifact browser evidence passes.