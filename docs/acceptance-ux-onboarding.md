# Chrome Store acceptance UX onboarding pass

Branch: `feat/acceptance-ux-onboarding`
Base: `main` at `d4d69ee3d35b5724e8615e8b4124887948770f3a`

This pass is scoped to customer acceptance from one starting instruction:

> Install the extension from the Chrome Web Store.

Preview remains independent. Full mode continues to fail closed when required desktop, commercial, or licensed-analysis authority is unavailable.

## Current authority model

The customer-facing sequence remains:

`creator/account provisioning complete`
→ `commercial authority required`
→ `customer-safe activation/reissue continuation`
→ `existing hosted + installation-key proof authority`
→ `commercial authority verified and durably stored locally`
→ `ofca-analysis-readiness/v1`
→ `licensed-analysis admission`
→ `Full mode is ready`.

`ofca-analysis-readiness/v1` is the canonical customer-facing Full-readiness source. Desktop reachability, authenticated companion transport, creator approval, a hosted navigation return, and an activation HTTP response are not completion signals by themselves.

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
- While the canonical commercial state is being read, the customer sees **Checking activation**.
- Missing commercial authority is shown as **Full activation required** with **Check activation**. Product does not expose a fake hosted activation action while no customer-safe hosted handoff exists.
- Verified local commercial authority with blocked licensed analysis is shown as **Full activation active**, but not Full-ready.
- Commercial authority that cannot be confirmed is shown as **Full activation needs attention** and is not described as an invalid license.
- **Full mode is ready** appears only when secure local delivery is authenticated, current commercial authority is active, and current licensed-analysis admission is admitted.

Desktop first-run setup remains customer language:

`Connect this computer` → `Confirm your creator account` → `Approve your creator account` → `Finish desktop setup`.

A browser or desktop restart resumes normal durable first-run checkpoints instead of returning the customer to the beginning.

## Gap 3 — customer-safe commercial activation/reissue continuation

### Product-side status

Implemented to the maximum extent supported by existing authoritative contracts:

- commercial authority and licensed-analysis admission are evaluated independently;
- accepted commercial authority can remain customer-visible as active even when the current analysis major/family is not admitted;
- the extension reads only the closed `ofca-analysis-readiness/v1` document over the authenticated companion channel;
- activation-required, activation-checking, activation-active/analysis-blocked, activation-unavailable, and licensed-ready states are customer-safe and contain no package/seat/license/issuance/proof/key/exchange fields;
- activation completion is never inferred from navigation, transport authentication, or a button click;
- Preview and existing durable desktop data remain outside the activation gate;
- the existing low-level activation/reissue endpoints and verifier/journal paths remain unchanged and protected.

The existing low-level Product delivery APIs still accept internal `package` and `seat_id` inputs and are intentionally retained as an integration seam. They are not wired to normal customer UI.

### Missing hosted → Product activation handoff

The existing hosted CapabilityLicense API can prepare encoded activation/reissue packages and can authorize replacement reissue. It does not currently expose a customer-safe continuation consumed by Product that transfers or redeems that authority without exposing the raw delivery package and seat coordinate.

To close the customer journey, Product needs an authenticated hosted continuation bound to the current customer/account and target installation that returns an opaque continuation/redeem result. That continuation must let the existing local delivery path obtain the authorized activation/reissue material without asking the customer to copy or manipulate package, seat, proof, signed-license, issuance, installation-key, or exchange fields. Product must then use the existing installation-key proof, production verifier, durable delivery journal/replay, and local authority binding. The customer-facing completion signal remains the resulting `ofca-analysis-readiness/v1` state after durable acceptance.

Until that contract exists, Product remains fail closed and exposes **Check activation**, not **Continue activation**.

### Reissue distinction

Product has no authoritative local state that distinguishes:

- new activation required; from
- replacement/reissue authorization required.

The replacement installation key alone does not authorize reissue, and Product does not infer replacement from installation age, missing authority, browser state, or customer action.

If customer copy such as **Move activation to this computer** is required, hosted authority must supply a customer-safe authoritative reissue disposition for the current customer/account and replacement installation. That is an external contract dependency; no UX-only heuristic was added.

## Truthful Full readiness

Brain evaluates customer readiness from the same durable identity/account and commercial authority used by licensed analysis admission, while preserving the distinction between commercial activation and analysis admission.

The closed `ofca-analysis-readiness/v1` status contains only:

- `commercial_authority = required | active | unavailable`
- `analysis_admission = blocked | admitted`

An active commercial authority no longer requires analysis admission to be admitted. Therefore:

`commercial_authority = active`
+
`analysis_admission = blocked`
→ **Full activation active**, not Full-ready.

Only:

`commercial_authority = active`
+
`analysis_admission = admitted`
→ **Full mode is ready**.

The status read does not cache or create analysis admission. Actual processing still requires the existing admitted current policy path. The status travels only through the authenticated companion channel after matching Agent/account authentication and exposes no signed commercial material or commercial identifiers.

The extension independently validates that closed response and refuses malformed, extended, or impossible combinations such as `admitted` without active commercial authority.

## Resumable first-run setup

Brain derives a read-only progress state from existing durable provisioning records rather than browser memory:

- `registration_required`
- `creator_confirmation_required`
- `creator_approval_pending`
- `finalization_ready`
- `recovery_required`

The browser restores the appropriate step after reload/restart. Association/account coordinates needed for protected follow-up requests stay hidden from visible copy.

If a claim submission has an unresolved hosted outcome, or durable state is ambiguous, setup enters **recovery required**. Mutation is disabled and the customer is explicitly told not to reuse the setup code. This avoids spending or replaying one-time setup material merely to reconstruct UI progress.

No schema migration or browser-owned provisioning/commercial authority was added.

## Hosted onboarding entry and v2 handoff

The Product-side customer entry remains release-owned through `app/core/customer-release.json`.

The checked-in development document is deliberately blank. Development/test composition may retain the existing local hosted-origin fallback, but a release-grade Agent/Brain artifact must have both hosted coordinates configured. No production coordinate is fabricated on this branch.

The production Brain side of installation handoff remains v2 and the Product-side source-contract compatibility work remains closed. This does not prove the external production hosted deployment currently serves the new customer entry or issues v2 packages.

## Creator approval

Product creator-approval continuation is closed on this branch:

- durable `creator_approval_pending` is represented explicitly;
- the hosted page may be opened only as a continuation;
- returning/navigation cannot grant approval;
- **Check approval** advances only through the existing authoritative binding-acquisition path after hosted approval exists;
- browser/Brain restart resumes durable pending/finalization state.

The actual hosted customer approval ceremony is an external parallel control-plane dependency and is not implemented or modified in this Product task.

## Existing data and offline behavior

Commercial authority gates new licensed processing only.

This Gap 3 work does not add commercial checks to:

- existing local creator data;
- existing durable analysis results;
- local export/delete/access functions;
- Preview.

After verified commercial authority has been durably stored, normal local startup/readiness reconstruction remains local. No hosted check was added solely for UX convenience.

## Evidence

### Readiness authority

`tests/test_analysis_authorization.py`

- activation required when no verified local commercial reference exists;
- commercial activation remains active when analysis is blocked by incompatible current analysis admission;
- active/admitted only when the independent analysis predicate succeeds;
- ambiguous commercial authority fails closed;
- readiness checks do not create cached analysis admission.

`tests/test_companion_analysis_readiness_rpc.py`

- fresh Agent authentication required;
- only the closed nonsecret readiness state is returned;
- cross-account identity is refused.

### Customer presentation

`extension/tests/customer-journey.test.mjs`

- authenticated transport alone renders **Checking activation**, never Full-ready;
- **Full activation required** does not expose a protocol handoff;
- activation-unavailable remains retryable and is not called an invalid license;
- commercial-active + analysis-blocked renders **Full activation active**, not Full-ready;
- Full-ready requires authenticated delivery + commercial active + analysis admitted.

`extension/tests/customer-activation-copy.test.mjs`

- normal activation surfaces are guarded against CapabilityLicense/package/seat/license/issuance/JWS/proof/key/exchange terminology.

`extension/qualification/customer-journey-visual.spec.mjs`

- deterministic visual coverage includes activation checking, required, active-with-analysis-blocked, unavailable, and licensed Full-ready.

Existing CapabilityLicense delivery/verifier/reissue/journal tests remain the authority evidence for production signature verification, installation/account/key matching, crash-safe replay, and hosted-authorized replacement semantics. Gap 3 does not weaken or duplicate those paths.

## Remaining P0 / external dependencies

1. **Gap 1B — production binding/deployment evidence.** Real release-owned hosted onboarding/API coordinates plus deployed-v2 proof remain open. Source tests are not production deployment evidence.
2. **Hosted creator-approval ceremony.** Product continuation is closed; the real hosted session-derived approval ceremony is owned externally/in parallel.
3. **Hosted commercial activation handoff.** Product needs the authenticated opaque hosted continuation/redeem contract described above before **Continue activation** can be offered.
4. **Hosted commercial reissue disposition.** Required only if Product must truthfully distinguish replacement/reissue from ordinary activation-required state.
5. **Exceptional unresolved-claim reconciliation.** The current UX correctly blocks replay when hosted consumption may have succeeded but local completion was lost; authoritative hosted reconciliation remains separate Gap 4 and is not started here.
6. **Authoritative desktop installer distribution.** A Store candidate still needs the real release-owned latest-supported desktop installer URL.
7. **Customer-recognizable creator label.** A safe handle/display label remains needed if customers must visually distinguish multiple creator accounts.
8. **Final exact-artifact clean-machine journey.** First licensed analysis from Store install remains blocked by the external production dependencies above.

## Validation status

- Pre-restack branch head: `2b299068114e0f4c3cb624c176e452c03de04133`.
- The branch was rebased onto `main@d4d69ee3d35b5724e8615e8b4124887948770f3a` before Gap 3 work.
- Product 2.0.3 release identity/qualification changes from PR #37 are preserved.
- Draft PR #36 remains the qualification path and must stay Draft.
- No signed integration commit has been created.
- No final Chrome Store package has been frozen.
- No production hosted coordinate has been invented.
- `main` and the control-plane repository are not modified by this Product task.

## Release position

Do not freeze or submit a new Store ZIP yet. Gap 3 Product semantics and fail-closed customer presentation are implemented to the maximum extent supported by existing contracts, but the customer-safe hosted commercial handoff/reissue disposition and Gap 1B production evidence remain external blockers. Do not begin unresolved one-time claim reconciliation Gap 4 as part of this slice.
