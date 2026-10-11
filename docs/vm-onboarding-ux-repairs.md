# VM onboarding review repairs

Source baseline: Product `dd4c9d8ff23b29f21bf631c4394c7beaea0d5d65`, the owner's
2026-10-11 VM review, and the approved Phase 4 reference/task plan.

## Changes

| Finding | Implementation | Principal regression |
| --- | --- | --- |
| UX-01 | Keep one receiving-code entry after the primary task, with its existing receiving route and validation. | `extension/tests/setup-progress.test.mjs`, `setup-transfer-ui.test.mjs` |
| UX-03 | Derive progress label, completion and review destination from one projection. Site permission is no longer presented as another mode decision. Explicit transitions focus and reveal the new heading. | `extension/tests/setup-progress.test.mjs` |
| UX-04 | Retain extension pause ownership without a confirmed desktop controller; label its setup controls Pause/Resume without claiming Full readiness. | `extension/tests/status-presentation.test.mjs`, `setup-progress.test.mjs` |
| UX-05 | Add executable display metadata and the existing approved extension artwork; update installer/protocol display strings without changing AppId, executable name or existing installation directory. | `tests/test_windows_branding.py` |
| UX-06 | Bound requests and browser ceremonies; distinguish stages without guessing cancellation causes; reconcile an uncertain login finish by one authenticated state read, never mutation replay. An unavailable initial status offers a status retry, not invented enrollment state. | `frontend/tests/webauthnApi.test.ts`, `WebAuthnAccessView.test.tsx` |
| UX-07 | Separate explicit helper closure from restart/replacement loss; track helper ownership within the browser session and rediscover existing usable tabs before opening a helper. | `extension/tests/document-observer.test.mjs` |
| UX-08 | Admit a genuinely fresh extension locator as initial enrollment, with kind and saved-target checks repeated under the write transaction. Keep registered continuation, expiry, retirement, conflict and uncertain-operation refusal. | `tests/test_packaged_boot_dispatch.py`, `test_native_workspace_recovery.py`, `test_saved_continuation_native.py` |
| UX-08b | A launch timeout remains unconfirmed, not proof of a closed app. Status renders cannot replay a failed navigation. | `extension/tests/desktop-launch.test.mjs` |

UX-02 is an intentional public legal access restriction and is not changed.
UX-09 is implemented separately in the Provider repository.

## Native return presentation and diagnostics

The callback uses shared semantic tokens, responsive local styles, keyboard focus
and live status feedback. Only fixed, nonauthorizing CSS resources are added.
CSP keeps scripts and styles local. The generated token stylesheet is aliased
into the allowed runtime asset directory at packaging time; frontend source
remains forbidden in the package.

The failure element records a closed stage name (`entry`, `workspace-prepare`,
`workspace-select`, `state-read`, or `workspace-return`) for diagnosis. It does
not record request bodies, tokens, account contents or exception text. A return
tab may close only after the existing verified navigation acknowledgement.

## Evidence and limits

The real composed application regression failed before the admission fix for
both targeted and discovered fresh extension UUIDs; the empty desktop-first
case passed. Refusal tests cover expired/retired locators, saved registered
targets even after old journey metadata was removed, uncertain operations,
replay and a kind-change race. This establishes a source defect and correction;
it does not identify the first failing request in the original VM capture.

Focused backend, native callback, complete extension, complete frontend,
packaging policy, architecture and CI-owner checks are run before publication.
The separate Provider change has its own test receipt and installation-link
preflight. Logs are retained on the implementation host under `C:\pt\vm-ux-*`.

Six isolated native-return source captures cover light/dark themes, desktop and
narrow layouts, and 200% enlarged text. Layout checks cover clipping, target
size, separated headings/feedback/actions and keyboard focus. These captures
use closed synthetic responses with no network listener or real account. They
are not installed onboarding evidence.

The previous candidate remains installed in the VM. This change does not merge,
deploy, reinstall, reset browser state or open the VM viewer. Before acceptance,
rebuild exact-source artifacts and repeat the complete VM journey, including
the Windows application prompt, fresh admission, Entra return, creator approval,
Windows Hello, pairing, commercial activation and confirmed licensed readiness.
The original returning-user Windows Hello failure still needs real-ceremony
classification; clearer recovery is not proof that its underlying cause is fixed.
