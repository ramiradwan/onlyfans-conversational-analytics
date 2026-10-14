<!-- CODE-VERIFY: app/packaged_entry.py app/core/first_run.py app/onboarding/admission.py frontend/onboarding/ frontend/src/routing/useAppRoutes.tsx desktop/src-tauri/src/policy.rs desktop/src-tauri/capabilities/desktop.json desktop/src-tauri/capabilities/window-frame.json app/onboarding/ docs/architecture-boundaries.json; qualify the native implementation against this decision before release. -->

# ADR 0050: Coordinate setup in the native desktop application

- Status: Accepted.
- Decision authority: Product owner, through approval of the desktop onboarding implementation plan.
- Supersedes: ADR 0047's single browser workspace and extension-owned Full presentation; ADR 0045's direct Chrome access from Bridge; ADR 0048's browser return presentation. ADR 0049's durable ownership, fencing and uncertain-outcome requirements remain authoritative.

## Decision

Bridge is one Windows Tauri 2 application. Brain owns one durable setup coordinator. Required setup progresses through Browser access, Account connection and Finish setup. Verified installation/account binding, enrollment and pairing admit the dashboard under existing local authentication. Commercial activation is a separate resumable dashboard/Settings task; missing or uncertain activation never makes completed local setup incomplete. Existing capability admission still governs Full analytics.

The native host owns its fixed-path Brain child, window, tray and single-instance activation. It verifies the listener's address, PID, image and user before admitting its fixed local origin, `http://bridge.localhost:17871`. Process exit invalidates that admission. Exit 75 permits one controlled configured restart; ordinary failure never creates a restart loop. Close hides the window; Quit stops the owned backend. No login autostart is added.

Preserve the configuration-selected boot graphs described by ADR 0017 without promoting its other proposed behavior. Unconfigured setup imports neither runtime settings nor the dashboard/analytics graph. Publish complete configuration atomically and never infer recoverability from malformed present configuration. Native possession is not operator authentication. Local WebAuthn retains its relying-party identity and origin.

Expose only declared purpose-specific native commands, with an explicit Tauri application command manifest and exact window/origin capabilities. Renderer inputs cannot select executable paths, profiles, arbitrary URLs, shell commands, filesystem contents or HTTP targets. External pages receive no native authority.

Admit the exact configured application paths used by Bridge's client router: `/`, `/inbox`, `/analytics`, `/graph-explorer` and `/settings`, alongside the isolated onboarding entry. Client navigation within the owned document retains its native presentation commands. The Rust document check and Tauri capabilities agree on those paths; browser helpers, setup-code documents, API endpoints and unknown paths remain excluded. The live owned-child admission, exact main window, operator authentication and command guards still apply.

The main window uses the app's styled header in place of the OS title bar. One header contains the app name, a noninteractive drag region and accessible minimize, maximize/restore and close controls. Startup and failure documents retain those controls. Window presentation commands remain separately scoped to the owned window and exact packaged shell or admitted local document; browser tasks gain no native authority. Status copy states the observed status and, where useful, a simple next action.

## Browser tasks

Chrome is the supported browser. Preview remains extension-only. Full intent is not collection consent. Consent, governed instruments, browser permission and permitted account observation stay with the extension's existing owners. A browser helper is a task surface with no overall wizard; it alone imports Chrome adapters.

Separate desktop presentation admission from browser task admission. Bind each to its role, journey, task when applicable, generation and fixed expiry. A replacement presentation fences its predecessor while retaining accepted work. Identifiers and app links are locators, not authorization. The `ofca://onboarding/?journey=<uuid>` app link uses Chrome's canonical root path and carries no account selection, credential or protected decision.

Before pairing, the verified native launcher issues a single-use task entry; Chrome supplies the existing top-level extension sender admission. Never copy a WebView cookie into Chrome or add an unpaired command socket. Browser tasks last at most 1800 seconds, bounded by their parent and operation deadlines; entry links last at most 300 seconds. Reads do not extend either. For the wrong profile, the customer switches profiles in Chrome and opens a newly issued task link. Reissue fences the old admission, preserves the protected operation and grants no account authority. The host does not inspect Chrome profile files.

This is an explicit additional exception to ADR 0047's launcher-only URL admission rule: a local browser-task entry may carry one opaque credential for its exact landing and task. Persist only its digest, consume it once, and remove it from the URL immediately. It cannot authenticate a general Bridge session or authorize a protected decision.

The browser participant must match saved Full intent and workspace evidence on extension-first entry. Desktop-first attachment requires the explicit browser-access action. Merely opening a helper does not choose a creator. The install event attaches to an existing admitted helper rather than creating another Full wizard.

After pairing, closed browser tasks and bounded acknowledgements use the authenticated companion session. Paused Full retains its control-only channel without capture or storage unlock. Pairing still requires Brain's constant-time comparison against the independently delivered extension-port code, scoped to that exact task. Existing manual comparison remains an explicit recovery route.

## Hosted ceremonies and durable coordination

Provider authentication, exact creator approval and commercial confirmation stay in focused hosted ceremonies. The extension uses `launchWebAuthFlow`; without an extension, the same ceremony runs in an ordinary Chrome tab. No Provider cookies, identity tokens, licenses or redemption continuations travel through the extension redirect. Brain reconciles the underlying operation through its existing proof authority. The cross-plane ceremony profile is owned by the canonical contracts repository; local presentation and browser task schemas remain Product-owned.

The ceremony result adapter requires a new, separately domain-separated installation-key proof profile. Existing browser-only activation reads and redemption proofs are not repurposed. Its activation result returns only the already committed, exact installation-bound `clr1` continuation within its original lifetime; it never mints or renews authority. Provider session cookies remain in the browser that completed authentication. Chrome's return is correlation only.

The product has not launched and has no existing users. Replace superseded onboarding admissions, routes and controllers without compatibility adapters. The corresponding Contracts and Provider decisions explicitly retire their old-client support clauses. Preserve domain authorities and restart/recovery behavior used by the replacement.

New ceremony records require individual retention classifications: expiring contexts and proof challenges, bounded notification correlations, unresolved worker outcomes, committed commercial authority and security events do not share one deletion timer. Existing domain retention remains authoritative.

Local task admission records are presentation working state: delete completed, cancelled and expired task records at their original admission deadline, and delete their unreferenced retired presentations. Preserve accepted or uncertain work for domain reconciliation. Task-creation limits must never prevent a fresh native presentation from reconciling existing work.

The coordinator delegates to existing domain services rather than duplicating their authority. Publish verified facts, separate local/Full readiness, pending operations and allowed actions. Subscribe before authoritative reads and retain invalidations received while busy. Commands carry unique operation IDs and exact expected scope/generation. Reads and renders do not initiate protected mutations. Closing a page or losing a reply cannot establish cancellation or justify another mutation.

## Confirmation

Qualify the installed native host, Windows Hello, scoped admission and provisioning restart before broad controller replacement. Cover hostile listeners, external navigation and native command denial; wrong profiles, stale presentations and entry replay; consent/permission gates, independent pairing, paused controls; duplicate actions, real worker cancellation, lost committed results and expiry; and dashboard access independent of activation. Keep offline authorization and all existing cryptographic, recovery and evidence semantics.

Architecture declarations classify the native host, coordinator and local schemas. The customer journey manifest changes with rendered states, not ahead of executable surfaces. [ADR 0056](0056-msix-packaging-and-managed-updates.md) supersedes the Inno-only packaging choice while preserving the complete frozen Brain distribution and shared WebView2. This decision grants no automatic live deployment authority.
