# Desktop onboarding startup dependencies

Source inspection: `ab062b6`. This document records current ownership and admission boundaries. It does not grant permission to change the startup order, to collect data, or to run text analysis.

## Entry and local setup

The native host and Brain own different parts of startup. A visible window, a bootstrap status frame and a healthy listener are different events; none of them establishes creator or browser authority.

| Work | Current owner and prerequisites | Side effects and overlap decision |
| --- | --- | --- |
| Windows prerequisite and storage admission | Native host; compatible WebView2, approved release identity, resolved data root, exclusive ownership. See [Desktop startup storage](desktop-startup-storage.md). | May inspect the machine and existing root; must refuse on unsafe or incompatible state. Preserve this admission ahead of WebView profile and Brain construction. |
| Brain storage admission | [Packaged entry](../../app/packaged_entry.py) and [startup maintenance](../../app/core/startup_maintenance.py); parent supervision and writer lease. | Inspection, verified checkpoint and migration where necessary. No second writer or startup process may access the same writable root. Do not overlap migrating constructors with admission. |
| Restricted setup application | `select_brain_application` in [packaged entry](../../app/packaged_entry.py) and [provisioning application](../../app/provisioning/app.py); no runtime configuration. | Opens the provisioning authority and starts the existing setup coordinator. Does not construct the configured analytics application. Do not start a second configured Brain ahead of configuration. |
| Browser access and account connection | [Desktop coordinator](../../app/onboarding/coordinator.py), [desktop setup owner](../../app/provisioning/desktop_onboarding.py) and [initial enrollment](../../app/provisioning/initial_handoff.py). Current desktop admission, accepted browser/account intent and current journey. | Browser actions, durable installation proof and hosted requests are scoped to the accepted operation. Provider selection and confirmation remain explicit. Existing [authentication prefetch](../../app/onboarding/authentication.py) prepares a proof read under the current parent guard; it is not speculative sign-in. Retain the existing overlap. |
| Local configuration | `setup.finalize` in the desktop coordinator and setup owner; verified installation and account, current generation/scope, single durable receipt. | Writes configured runtime material and triggers the existing controlled completion path. Automatic finalization is already admitted by the coordinator. Do not pre-create account configuration. |
| Configured application entry | [Packaged entry](../../app/packaged_entry.py) selects the configured app from the committed runtime configuration; [native host](../../desktop/src-tauri/src/host.rs) owns the child and controlled restart. | One owned process and one writer. Retain the configuration boundary; a speculative parallel runtime would violate ownership. |
| Passkey and pairing | [Desktop coordinator](../../app/onboarding/coordinator.py), [pairing owner](../../app/onboarding/pairing.py), [desktop enrollment presentation](../../frontend/onboarding/enrollment.js). Configuration before enrollment; verified local access, fresh authenticated snapshot and explicit pairing intent before connection. | Security gestures and replacement decisions cannot be performed speculatively. Dashboard admission is based on verified installation, account, configuration, enrollment and pairing. |

## Configured runtime startup

The current sequence is defined in [application startup](../../app/main.py):

```
configured application construction
  -> optional standalone dependency startup (if configured)
  -> installation key and runtime activation evaluation
  -> broadcast connection -> transport startup
  -> launch grant renewal
  -> signing-parameter service startup
  -> desktop coordinator startup (if attached)
  -> configure analytics runtime
  -> retention/deletion admission
  -> launch derived-state recovery
  -> startup complete
```

The arrows describe the observed invocation order, not a proof that every adjacent task depends on the preceding one.

| Work | Actual owner and constraints | Candidate decision |
| --- | --- | --- |
| Installation key and activation | `activate_runtime` and `initialize_installation_key` in application startup. Local-session mode needs the governed key; unavailable key can retain the restricted surface but does not confer activation. | Do not move runtime admission earlier. Account-independent CPU or filesystem probes are not a substitute for activation. |
| Broadcast and transport | Application startup connects the broadcast owner then starts the transport manager. | Keep existing order until lifecycle and admission dependencies are proven. Do not open a second transport or duplicate session. |
| Grant renewal | `start_grant_refresh` in application startup and [grant refresh](../../app/security/grant_refresh.py). | Already launches its own background owner when eligible. No need to add another worker. |
| Signing parameters | [Signing parameter service](../../app/services/signing_parameters.py); configured store, reviewed trust and persisted checkpoint. Startup verifies disk state, may initialize persistence, and schedules refresh separately. | Potential configured-startup candidate, not a read-only pre-configuration probe. Establish transport availability, failure and shutdown behavior plus cold timings before changing invocation order. |
| Retention and pending deletion | [Retention maintenance](../../app/services/retention_maintenance.py), current canonical database and derived deletion owner. | Initial sweep and pending deletions are awaited before readiness. Preserve this fail-closed barrier; no stale or expired data may become available by moving it behind readiness. |
| Derived-state recovery and questions | [Analytics runtime](../../app/analytics/runtime.py), one projection scheduler and configured persistence. | Already launched in the background after retention admission. Do not add a second scheduler or bypass the projection activation fence. |
| Installed text-analysis resources | [Dependency setup](../../app/services/nlp_setup.py), [dependency service](../../app/services/nlp_dependencies.py) and [bound dependency runtime](../../app/analytics/nlp_dependency_runtime.py). Requires admitted release/resource policy and the applicable bound store. Startup revalidates disk, recovers operations and may remove interrupted material; the configured bound owner already starts in the background. | Not a generic read-only preflight. No speculative download, warmup, new resource owner or analysis before the appropriate policy and execution admission. |

## Collection and first results

Commercial authority, analysis admission and message collection are separate facts. See [desktop readiness](../../app/onboarding/readiness.py), [browser consent](../../extension/runtime/consent-controller.mjs), [history coordinator](../../extension/transport/read-only-history-coordinator.mjs) and [canonical coverage](../../app/persistence/history.py).

- Passive capture requires the existing effective collection gate and bound account/session. History requires its separate accepted settings, permission and applied configuration. Neither starts solely because a dialog is displayed.
- The history owner discovers and freezes the inventory before committing conversation coverage evidence. Brain explicitly rejects premature conversation evidence. Retain this ordering and the existing allowance and lease; changing it would require a separately qualified owner/coverage design.
- Committed history pages now notify the existing ordered transport immediately, with normal acknowledgment, snapshot repair and heartbeat fallback. This removes a delivery wait, not a request budget or admission gate.
- A matching activated partial projection may be rendered before complete history. The dashboard does not need to wait for completion.
- Text analysis retains the existing execution consent, profile eligibility, resource and saved-result checks. The existing scheduler publishes results, and the authenticated publication stream notifies the analytics view. No additional execution is triggered by that stream.

## Measurements required before concurrency changes

Measure with one monotonic clock per collector, recording the scope and the first actual observation of each applicable boundary. Keep independent user-decision and external-owner intervals identifiable. Do not add account identifiers, messages, credentials, raw requests or paths to the timing receipts. Omit non-applicable milestones instead of assigning them zero duration.

| Interval | Measurement boundary |
| --- | --- |
| Entry to first usable setup | Native entry, owned process startup, validated listener, visible first actionable task. |
| Setup to local readiness | Browser available, verified identity/account, committed configuration, configured child readiness, enrolled passkey and verified pairing. Record external waits and deliberate gestures separately. |
| Collection readiness to first activity | Effective authorized collection, first eligible committed message, matching activated projection, visible nonzero result with valid coverage. |
| Analysis admission to first saved finding | Last applicable data/resource/execution prerequisite, saved result publication, matching visible finding. Never equate activity with a text finding. |

The source-browser first-finding test in [text-analysis processing](../../tools/e2e-capture/tests/text-analysis-processing.spec.mjs) measures confirmation to visible saved finding on already-saved synthetic data. It does not cover native installation, live hosted operations, real model latency, or first acquisition. The existing [passive capture browser test](../../tools/e2e-capture/tests/capture.spec.mjs) proves real extension delivery and projection on synthetic platform traffic; a first-activity timing receipt still needs to measure the visible dashboard boundary.

For an ordering candidate, run matched cold baseline and candidate fixtures with separate writable roots, equal release/resource/profile inputs and the same scripted user decisions. Record distributions, not just a single run. Check cancellation, restart, missing prerequisites, duplicate owner prevention, unauthorized acquisition and retention before readiness, plus actual improvement without input/UI starvation. Existing source and packaged qualification routes remain authoritative. A missing prerequisite or unmeasured overlap is not a passed qualification.

**Current decision:** no startup concurrency change is authorized by this inspection alone. The existing background owners and guarded authentication preparation are already in use. Complete the cold journey trace and compare any proposed order change against it.
