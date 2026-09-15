# Chrome Store acceptance UX onboarding pass

Branch: `feat/acceptance-ux-onboarding`
Base: `main` at `01461b598aacdcd54d33bb5abc93f687225a4780`

This pass is scoped to customer acceptance from one starting instruction:

> Install the extension from the Chrome Web Store.

Preview remains independent. Full mode continues to fail closed when required desktop or production authority is unavailable.

## Before / after journey

### Before

`Install extension` → `Open popup` → Preview is usable.

Choosing Full could send the customer to an unavailable local page. Desktop first-run setup exposed architecture-oriented steps, intermediate browser state was not resumable, and authenticated local delivery could be presented as if paid Full analysis were ready.

### Current branch

`Install extension` → `Open popup` → one readiness card explains the current state and next step.

- Preview works without the desktop app and makes no desktop readiness probe.
- Full requested with no reachable desktop app explains that the desktop app is required rather than opening a dead local page.
- A previously connected but stopped desktop app has a distinct retry state.
- Missing signed-in creator context is explained before connection controls are offered.
- The extension and desktop app use the existing authenticated connection ceremony with a six-digit comparison.
- Connection cancellation, mismatch, timeout, and status-check failures have explicit recovery.
- Authenticated local delivery is shown as **Desktop connected**, not as paid Full readiness.
- Missing commercial authority is shown as **Activate Full analysis**.
- Commercial authority that cannot be confirmed is shown as **Full activation needs attention**.
- **Full mode is ready** appears only when secure local delivery is authenticated, current commercial authority is active, and current licensed-analysis admission is admitted.

Desktop first-run setup uses customer language:

`Connect this computer` → `Confirm your creator account` → `Approve your creator account` → `Finish desktop setup`.

A browser or desktop restart now resumes normal durable first-run checkpoints instead of returning the customer to the beginning.

## Truthful Full readiness

Brain evaluates customer readiness from the same current identity/account and CapabilityLicense predicates used by licensed analysis admission.

The closed `ofca-analysis-readiness/v1` status contains only:

- `commercial_authority = required | active | unavailable`
- `analysis_admission = blocked | admitted`

The status read does not cache or create analysis admission. Actual processing still requires the existing admitted current policy path. The status travels only through the already-authenticated companion channel after matching Agent/account authentication and exposes no CapabilityLicense bytes, packages, grants, tickets, seat IDs, license IDs, issuance IDs, or reference IDs.

The extension independently validates that closed response and refuses malformed, extended, or impossible combinations such as `admitted` without active commercial authority.

## Resumable first-run setup

Brain now derives a read-only progress state from existing durable provisioning records rather than browser memory:

- `registration_required`
- `creator_confirmation_required`
- `creator_approval_pending`
- `finalization_ready`
- `recovery_required`

The browser restores the appropriate step after reload/restart. Association/account coordinates needed for protected follow-up requests stay hidden from visible copy.

If a claim submission has an unresolved hosted outcome, or durable state is ambiguous, setup enters **recovery required**. Mutation is disabled and the customer is explicitly told not to reuse the setup code. This avoids spending or replaying one-time setup material merely to reconstruct UI progress.

No schema migration or browser-owned provisioning authority was added.

## Implemented customer-facing changes

### Extension

- Prominent current-state/next-step card.
- Preview independence preserved.
- Desktop-required and stopped-desktop recovery states.
- Setup-incomplete state when creator context is missing.
- Progressive disclosure of connection controls.
- Clear six-digit connection comparison and recovery.
- Separate **Desktop app**, **Secure connection**, **Full activation**, and **Licensed analysis** status rows.
- Full-ready reserved for licensed readiness, not transport authentication.
- Accessible focus treatment, forced-colors/reduced-motion support, bounded popup dimensions, and responsive width behavior.

### Desktop first run

- Customer goals instead of architecture terminology.
- Visible “Setup code” language instead of “Installation package.”
- No raw creator account identifier in visible copy.
- Approval pending and hosted/offline failures have specific recovery guidance.
- Durable resume after registration, account confirmation, and approval.
- Interrupted/ambiguous claim recovery prevents code replay.

### Normal desktop connection screen

- **Connect browser extension** instead of architecture-heavy pairing language.
- No raw creator account ID or extension identity thumbprint shown to the customer.
- Six-digit code is the customer confirmation signal.
- Customer-language actions for open, confirm, check, disconnect, and refresh.

## Remaining P0 dependencies

These are not bypassed on this branch.

1. **Authoritative desktop installer distribution.** A Store candidate still needs a release-owned latest-supported desktop installer URL. No production URL is available in this repository today, so none is invented.
2. **Authoritative hosted onboarding entry.** The desktop app still needs a supported customer handoff into the hosted setup surface rather than documentation or an engineer-provided route.
3. **Cross-repository installation handoff compatibility.** Hosted onboarding and Brain must emit/accept the same production installation package profile. Brain must not weaken its decoder to hide a mismatch.
4. **Creator approval continuation.** The desktop UI can represent approval pending and resume it, but the hosted customer surface must provide the actual approval ceremony.
5. **Commercial activation continuation.** The product can now truthfully detect `Activation required`, `Activation unavailable`, and `licensed ready`, but it still needs a customer-facing hosted activation/reissue continuation that does not expose package/seat terminology.
6. **Customer-recognizable creator label.** Current contracts expose an internal account coordinate; the UI intentionally hides it. A safe handle/display label is required if customers must distinguish multiple creator accounts visually.

Installed-versus-stopped before any prior connection remains a lower-priority contract question. Current copy avoids guessing: first-time state says the desktop app is needed; after a saved connection it can truthfully say the desktop app is not running.

## Tests and evidence

### Readiness authority

`tests/test_analysis_authorization.py`

- activation required without compatible commercial authority;
- active/admitted only with current compatible authority;
- ambiguous authority fails closed;
- readiness checks do not create cached analysis admission.

`tests/test_companion_analysis_readiness_rpc.py`

- fresh Agent authentication required;
- only closed nonsecret state returned;
- cross-account identity refused.

`extension/tests/analysis-readiness-client.test.mjs`

- readiness is queried only after the authenticated companion handshake;
- malformed, extended, or impossible ready responses fail closed.

`extension/tests/customer-journey.test.mjs`

- transport authentication alone is never Full-ready;
- activation required/unavailable are distinct;
- active commercial authority alone does not imply analysis admission;
- Full-ready requires authenticated delivery plus commercial and licensed-analysis readiness.

### Provisioning restart/recovery

`tests/test_provisioning_progress.py`

- registration, creator confirmation, approval pending, and finalization-ready progress;
- unresolved claims and ambiguous active candidates fail to recovery.

`app/provisioning/provisioning-resume.test.mjs`

- browser restart resumes after registration;
- approval pending resumes without re-querying or displaying internal IDs;
- approved setup resumes finalization using hidden durable coordinates;
- uncertain state disables mutation and says not to reuse the setup code.

`tests/test_provisioning_resume_browser_module.py` keeps the resume browser module in ordinary CI while the historical provisioning suite remains intact.

### Existing production-shaped browser coverage

The Windows browser E2E assertions were aligned to reviewed customer copy while retaining their underlying authority checks. The provisioning browser scenario still proves configured runtime identity after WebAuthn; Preview still proves no local-service traffic; the Full journey still uses the production-shaped authenticated companion path.

`extension/qualification/customer-journey-visual.spec.mjs` now contains deterministic visual states for Preview, desktop required, setup incomplete, connection compare/failure, stopped desktop, activation required, activation unavailable, and genuinely licensed Full-ready.

Fixture screenshots are not final release evidence. The final Store candidate must generate fresh exact-artifact browser evidence after all remaining P0 dependencies are closed.

## New-user acceptance checklist

Starting from a clean supported machine and the Chrome Web Store:

- [x] Understand and use Preview without the desktop app.
- [x] Understand that Full requires the desktop app.
- [ ] Install the latest supported desktop app from an authoritative in-product link. **Blocked: release distribution.**
- [x] Understand desktop first-run steps without architecture terminology.
- [x] Resume normal durable first-run checkpoints after browser/desktop restart.
- [ ] Enter hosted secure setup from an authoritative product handoff. **Blocked: hosted entry.**
- [ ] Complete installation registration against one compatible production handoff profile. **Blocked cross-repository.**
- [x] Detect/confirm the signed-in creator account without asking the customer to type an internal ID.
- [ ] Complete creator approval from the connected hosted customer surface. **Blocked: hosted continuation.**
- [x] Distinguish secure desktop connection, Full activation, and licensed-analysis admission.
- [ ] Complete commercial Full activation without internal package/seat terminology. **Blocked: hosted activation continuation.**
- [x] Show **Full mode is ready** only when current commercial authority and licensed analysis admission are confirmed.
- [x] Recover from normal connection and setup failures without generic “Something went wrong” copy.
- [ ] Prove first licensed analysis from only “Install this extension from the Chrome Web Store.” **Blocked by the remaining external P0s.**

## Validation status

- Active branch remains `feat/acceptance-ux-onboarding`, based on `main@01461b598aacdcd54d33bb5abc93f687225a4780`.
- Draft PR #36 remains the qualification path and must stay Draft.
- No signed integration commit has been created.
- No final Chrome Store package has been frozen.

## Release position

Do not freeze or submit a new Store ZIP yet. Truthful paid readiness and normal restart resume are implemented, but release/control-plane handoffs and exact-artifact acceptance remain open.
