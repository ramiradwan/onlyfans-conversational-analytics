# Local onboarding contract v1

Authority: [ADR 0047](adr/0047-persistent-onboarding-workspace.md).
Artifacts: `shared/onboarding/schema.json`, `vectors.json`, `manifest.json`.
Generator: `python tools/generate_onboarding_contract.py`; integrity check: append `--check`.
Independent consumers: Python jsonschema and browser-side Ajv validate identical vectors.

Every document is a closed object, at most 4096 UTF-8 bytes. Reject duplicate JSON keys,
unknown profiles, fields, enums, non-finite numbers, and values outside JavaScript's
safe integer range before dispatch. A profile match is a shape check, never authentication.
Display copy is not sent over these contracts. Reason codes map to reviewed local copy.

| Profile | Producer / consumer | Classification and lifetime |
| --- | --- | --- |
| `local-onboarding-state.v1` | Authenticated Brain or extension / local projections | Local workflow projection; in memory while channel is current, invalidated on disconnect |
| `local-onboarding-command.v1` | Local authenticated user intent / named owner | Local command; bounded owner operation ledger, retain for reconciliation until operation expiry |
| `local-onboarding-result.v1` | Authenticated named owner / requesting projection | Local result; same operation lifetime, never hosted |
| `local-onboarding-result.v2` | Authenticated named owner / requesting projection | Additive command/request and committed-generation scopes; same bounded local lifetime |
| `local-installation-discovery.v1` | Extension / loopback discovery adapter | Non-authorizing hint, not retained, rate-limited to one accepted hint per second per connection |
| `local-onboarding-workspace.v1` | Workspace coordinator / local UI | Non-authorizing UI draft; clear on completion or abandonment, at most 30 days idle |
| `local-onboarding-continuation.v1` | Brain / registered local destination | Non-authorizing restart/enrollment locator; bound to existing setup UI session, at most 30 minutes |
| `local-first-enrollment-result.v1` | Local enrollment endpoint / same-origin browser | Local CSRF secret; session lifetime, no logs, persistence, URL, or hosted transfer |

The workspace wire profile wraps the coordinator's local versioned record. The
coordinator also stores tab/window IDs in session storage; those IDs are never sent to
the hosted service. Its internal scope ID is not a creator identifier. Build-time route
IDs resolve to one exact origin and path; only a UUID reference is permitted in the hash.

`source` is chosen by the authenticated adapter, not trusted from a message. Before an
epoch changes, establish a new authenticated subscription and require a snapshot.
Snapshots replace complete per-owner facts; events carry the same full fact set at the
next committed revision. Never merge facts across owners or compare their revisions.
Account and consent generations cannot move backwards within an epoch. A gap makes the
projection uncertain and requests replay or a snapshot. Subscribe and buffer before
fetching a snapshot so changes during the request are drained afterwards.

A command ID is idempotent only for identical owner, action, and generations. An owner
rechecks current authority at commit. Confirmation requires a matching source, epoch,
operation, generations, and committed revision. A transport acknowledgement is not a
result. A lost channel changes pending operations to unknown. Results cannot overwrite
a terminal result or make a stale account current. The projection reference module
models these rules; adapters must validate the schema before calling it.

Generation-changing commands negotiate `local-onboarding.command-result.v2`. A v2
result's `command_epoch`, `command_account_generation` and `command_consent_generation` identify
the exact original request. Its unprefixed generation fields and revision identify
the committed owner snapshot. Confirm only when both scopes match their respective
records, the account remains current, and the committed revision is visible. Pause
and resume can rotate consent; that never permits confirming a command against a
different creator. Unknown outcomes may later reconcile from an owner result, but
the client never replays the command to obtain that result. Existing v1 fields and
semantics are unchanged. Owners that lack v2 cannot advertise this automation.

The negotiated named port accepts a command only in the closed wrapper
`{type:"command", epoch, command}`. The owner rejects a different epoch before
examining generation counters; raw v1 mutations on this port are refused. A
read-only `{type:"operation", operation_id}` reconciles the durable receipt
without replaying the action. A missing receipt returns the closed envelope
`{type:"operation", operation_id, status:"unavailable"}` and leaves the outcome
unknown. Receipts are bounded to the setup lifetime. Across worker restart,
generation counters cannot be compared: the owner checks durable account,
consent and effect evidence before issuing a result in its current epoch. The
projection still requires the exact original command epoch and generations.

Authenticated local event streams may also emit `workspace-focus` with the
closed body `{journey_id}`. A matching local page can ask the extension to focus
the existing admitted workspace through `{type:"focus"}`; this cannot navigate,
open a tab, or establish authority. The port replies `{type:"focus", focused}`.

`verified` means verified by the fact's owner. It does not authorize other components
to skip their own checks. `unknown` must never render as Ready. Snapshot/replay requests
use existing authenticated transports; discovery contains no account or authority data
and cannot trigger consent, pairing, or enrollment by itself.

Negotiation is explicit for `local-onboarding.v1`, `persistent-workspace.v1`,
`first-enrollment-session.v1`, and the signer's `captureMode: observe-only`. These are
new capabilities; absent support is not success. Legacy protocol fixtures are unchanged.
The only hosted bridge is its separately pinned allowlisted projection. Local files and
fixtures must never be vendored into the commercial service as inbound contracts.

## Native workspace recovery

Native recovery uses separate closed adapter messages. They do not change the
shared projection profiles or turn extension storage into local authority.

The exact top-level `/provisioning/native-return` document first reads its
cookie-bound native entry. A fresh entry supplies `entry_id` and a same-origin
CSRF value. Targeted entry also supplies its launcher-bound `target_journey_id`;
untargeted entry omits that field. Only the local selection request receives the
CSRF value. With no extension workspace, a callback matching that target may
recover the exact prior journey locally. An absent target, missing record or
generic extension messaging failure cannot establish recovery eligibility.

| Message | Fields besides `type` | Successful result |
| --- | --- | --- |
| `ofca.workspace.recovery-prepare.v1` | `entry_id` | `{status:"recovery_ready", recovery_id, previous_journey_id}` |
| `ofca.workspace.saved-continuation-prepare.v1` | `entry_id` | `{status:"recovery_ready", recovery_id, previous_journey_id}` |
| `ofca.workspace.recovery-reattach.v1` | `entry_id`, `recovery_id`, `previous_journey_id`, `journey_id` | `{status:"reattached"}` |
| `ofca.workspace.recovery-return.v1` | `entry_id`, `recovery_id`, `previous_journey_id`, `journey_id`, `route:"provisioning"` | `{status:"returned"}` for another existing owner, or `{status:"continued"}` when the callback becomes the owner |

Identifiers are UUIDs. Replies use `{ok:true,result}` or `{ok:false,code}`.
Ordinary recovery preparation may instead return `{status:"launch_pending",journey_id}` for a
current ordinary launch, or `{status:"launch_expired"}` after verifying the
unchanged extension workspace that provides a fresh explicit launch action.
Neither result renews expired launch authority.

The recovery intent lives in extension session storage for at most five minutes.
It binds the prior journey, creator/disclosure draft scope, callback tab/document
and existing owner tab/document or verified absence. A conflicting owner,
changed scope or unqualified replacement document invalidates it. Saved
continuation alone permits explicit reattachment after the callback has read its
committed local selection and current local state. Reattachment requires the same
callback tab and exact URL, unchanged intent identifiers, owner and scope, and the
original deadline. A prepared intent may bind the selected target once. A dispatched
intent may only reconcile that same target and its current committed document;
reattachment cannot reset its phase or repeat navigation. No new browser permission
is required. The callback verifies the backend result before requesting return;
entry and recovery identifiers alone authorize no session or provisioning action.

`POST /api/v1/provisioning/native-entry` accepts either the ordinary closed body
`{journey_id}`, the recovery body `{journey_id:prior,recover:true}`, or the saved
continuation body `{journey_id:prior|null,continue_saved:true}`. The latter is allowed
only when the preceding native read offered `continue_saved:true` and retained an
exact current saved target. An untargeted entry may accept null when no extension
workspace exists; its result omits `previous_journey_id`. A provided prior UUID is
navigation correlation, and any retained scope for it must match the saved target.
The target cannot be reselected through the ordinary null variant. All fresh
registered admissions preserve protected initial work through the final commit.
Recovery
requires a fresh native-entry cookie, exact origin, matching CSRF and targeted
prior context within the original bootstrap deadline. A selected recovery returns
`{state:"selected",journey_id:resolved,previous_journey_id:prior}`. A subsequent
GET exposes that selection only to the issued or retained browser session.
An uncertain POST is reconciled by GET; it is never replayed.

With extension coordination, return refreshes only the exact setup document to
obtain its selected local context, or adopts the callback when no setup owner
remains. A lost return reply
reconciles actual ownership without repeating navigation. The extension retains
matching draft choices, and the destination authenticates its selected journey
independently. Recovery never navigates an OnlyFans tab or brings a delayed
callback to the foreground after the user has switched away.

When the extension is absent, a same-origin browser channel can coordinate
return to a responding prior setup document. Its identifiers are navigation
correlation, not credentials. The receiver verifies the actual cookie-selected
mapping and its unchanged prior document before retiring old operations and
refreshing. The new document authenticates that mapping before acknowledging.
Multiple offers refuse; no offer before dispatch allows the callback to continue
and permanently ignore late offers. After dispatch, an absent acknowledgement
allows only read-only receipt reconciliation. It cannot trigger another
navigation or make the callback a second owner. This mechanism does not prove
that dormant physical tabs are absent or guarantee browser focus.
