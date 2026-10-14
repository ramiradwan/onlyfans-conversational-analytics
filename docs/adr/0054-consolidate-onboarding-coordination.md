<!-- CODE-VERIFY: app/onboarding/composition.py app/onboarding/task_reports.py app/onboarding/browser_routes.py app/onboarding/companion.py app/onboarding/readiness.py app/provisioning/setup_sources.py app/provisioning/desktop_onboarding.py shared/desktop-onboarding/schema-v2.json extension/runtime/authentication-task-session.mjs extension/runtime/browser-surface.mjs frontend/src/services/desktopOnboarding.ts; qualify both boot roots and transport adapters before release. -->

# ADR 0054: Consolidate onboarding coordination

- Status: Accepted.
- Decision authority: Product owner, through the instruction to implement the reviewed architecture simplification proposal.
- Complements: ADRs 0049–0053. Their authority, admission, recovery, consent and fixed-lifetime requirements remain authoritative.

## Decision

Construct the common onboarding services once through an owned desktop setup core. Restricted and configured composition roots supply their different finalization and runtime authority adapters. Restricted setup retains its isolated import graph. Runtime activation retains its separate continuation that preserves the current installation binding.

The core closes its coordinator and domain owners once. Cancelling a shutdown waiter does not cancel the owned drain. Shared construction introduces no global service locator, additional writer or process, new credential, or hosted endpoint.

Share authentication-task reports behind separate helper and paired admission adapters. Each adapter rechecks its exact current task and generation inside the existing owner transaction. Notification correlation permits a read of original work; it does not confirm a decision. Closure reaches only the existing selected-child withdrawal owner, with no borrowed operator authority.

Share the extension's authentication status subscription, deduplication, expiry and attachment release. The helper retains exact top-level document admission. The paired adapter retains its authenticated account, consent and control-channel checks. Reads and reconnects never launch authentication.

Pairing remains a separate purpose. Brain's comparison completion does not permit disconnection before the Agent commits its authenticated pin and reports exact completion. Helper finish targets its admitted document. Native focus intent remains bounded presentation state and does not establish domain completion.

Expose provisioning source lifecycle through a typed owner interface. Active sources, retained recovery records and retired evidence keep their original bindings and deadlines. A missing record or elapsed presentation does not prove noncommit. The current domain owners still validate terminal results, completion, key/account scope and protected work.

Keep the physical recovery and retirement records and conservative protected-work scan. Its classification generation remains checked in the admission transaction. Typed presentation of evidence does not replace current domain authorization or introduce a cached negative pending-work index.

Consolidate commercial and analysis readiness as a non-authorizing read projection and shared frontend selectors. Preserve creator-only disclosure and distinguish authorization, permission, acquisition and saved coverage. Keep existing browser transports. Version closed read shapes where required while preserving released bytes and current command generation, revision and scope-digest checks.

Native snapshots use the closed v2 read profile; commands retain v1 and their existing guard inputs. The browser-surface v2 profile carries only a bounded owner-reported history failure code through the existing authenticated channel. Historical protocol fixtures retain their bytes when this optional display field is absent. Readiness and acquisition status cannot authorize a command or infer completed coverage.

Narrower command freshness and a preparation-use-case rewrite are conditional follow-ups. They require their own evidence and review before changing admission policy or qualified commercial behavior.

## Confirmation

Verify actual construction and shutdown through both boot roots. Retain restricted import checks, specialized activation continuation, exact task/callback admission, paused controls, stale generations and child-closure races.

Exercise delayed authenticated pairing commit and attachment loss through both transport paths. Compare active, retired, terminal, completed and uncertain source behavior against real persistence transactions. Preserve unresolved receipts and original deadlines across restart and expiry.

Readiness consolidation must remove duplicate component fetch controllers while preserving explicit redemption, account scoping, owner invalidation, bounded reconnect and subscribe-before-read behavior. Requalify affected installed journeys against matching artifacts; source tests and earlier candidate receipts do not establish installed qualification for changed source.
