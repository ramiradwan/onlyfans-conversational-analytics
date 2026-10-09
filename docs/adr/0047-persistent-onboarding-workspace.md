# ADR 0047: Keep onboarding in one persistent browser workspace

- Status: Accepted.
- Date: 2026-10-08
- Decision authority: Product owner.
- Amends: ADR 0045, Desktop port opening behavior, Pushed state's browser-port liveness inference, and Controls after pairing's popup ownership. Other pairing and authorization guarantees remain unchanged.

## Decision

One journey uses one persistent setup tab per browser profile. Extension, hosted setup,
local provisioning, and Bridge are trusted destinations within that workspace, not
parallel wizards. The toolbar and desktop launcher reuse it. Only explicit opening
requests focus it. Installation discovery and background updates never steal focus.
Installers, identity-provider pages, browser permissions, and operating-system
authentication can temporarily open their required external surfaces.

The extension coordinates the tab once installed. Before installation, Brain retains
the journey reference for its browser setup page. Installing the extension adopts
that exact registered page. If its existing document cannot acquire extension APIs,
the coordinator navigates that setup tab to the extension destination. This never
navigates or reloads an OnlyFans tab. A workspace UUID is correlation, not authority.
Arbitrary return URLs, credentials in navigation, and broad browser-history access
are prohibited. Routes are an exact build-time origin/path allowlist.

UI drafts persist in extension storage with an instrument digest and creator scope.
Changed instruments clear acknowledgements; a changed creator clears the Full choice.
Stale documents cannot overwrite a newer scope. Checkbox drafts never establish
legal acceptance, browser permission, account identity, pairing, or commercial rights.
Returning after a reload, discard, or restart reconciles authoritative facts before
choosing the next unmet step. Browser storage does not establish authority.

A bounded local presentation marker links the workspace journey, creator-scope UUID
and disclosure digest to a domain-separated digest of the independently observed
account (or unknown). It is not part of the shared wire record and grants no rights.
Workspace entry, draft and native-launch requests wait for restored consent and current identity reconciliation.
A matching marker preserves the draft across worker restarts. Missing, invalid or
mismatched markers rotate the scope and clear only the creator-bound Full choice;
general acknowledgements remain until their disclosures change. Scope and marker
are submitted in one storage call, but no transactional browser-storage guarantee
is assumed: either partial half disagrees with the other and fails closed next time.
Identity writes publish a committed change after persistence as well as invalidating
capture immediately beforehand. This prevents delayed notifications from changing
an otherwise valid launch scope only when an unrelated tab opens. Workspace expiry
and local-data deletion also remove the marker.

Finish extension consent and browser permissions before opening a live pairing
attempt. Leaving Bridge still cancels its port-owned attempt under ADR 0045. A pending
UI intention can survive navigation, but an old attempt or comparison code cannot.
Same-browser pairing uses the verified browser path and requires no pasted code.
Cross-device setup codes are separate from companion comparison codes.

An open page or extension port is never evidence that Brain is running. Brain liveness
requires its authenticated connection. Disconnection immediately makes its projection
uncertain. An app link requires a user gesture; manual-launch help appears when the
connection remains unconfirmed. Focus changes are not launch evidence.

## Authority and state

| Owner | Facts and actions |
| --- | --- |
| Brain | Local installation, enrollment/session, pairing, verified activation, analysis readiness |
| Extension | Mode, consent, browser permissions, observed creator, attachment, helper ownership, capture |
| Hosted service | Hosted identity, membership, creator approval, commercial authorization |
| Bridge | Presentation and user intent; no independent authority |

The local bundle in `shared/onboarding` has separate per-owner snapshot/event profiles,
generation-bound commands and owner results, discovery hints, workspace records, and
local continuations. It is not a hosted API. Hosted projections use their separately
pinned closed contract and cannot receive local snapshots, commands, browser state,
CSRF tokens, runtime sessions, content, or telemetry.

Publish after commit. Authenticate the source before selecting its epoch, subscribe
before snapshot reconciliation, and buffer changes during reads. Revisions are ordered
within one source and epoch, never globally compared. A gap requests replay or a fresh
snapshot. An old source epoch cannot reactivate itself by sending a larger revision.
Every pending command has an operation ID and account/consent generations. Delivery is
not confirmation. Unknown outcomes retain uncertainty until the owner reconciles them.

Pause/resume rotates the consent controller's
real capture scope. Preserve this fencing and add `local-onboarding-result.v2`
rather than change v1 semantics. The result identifies both the original command
scope and the committed scope/revision. A client requires the negotiated
`local-onboarding.command-result.v2` capability, a matching original operation,
the current committed scope and revision, and the same current account before
confirming it. No result can revive a previous account or imply a missing commit.
The additive schema and negative vectors are generated with the local contract;
no hosted profile or legacy desktop/runtime document changes.

Commands on the negotiated v2 port also name the authenticated owner's epoch.
Receipts identify that original epoch and the current committed epoch separately;
counter values cannot be compared across worker restarts. Reconnect performs a
read-only receipt lookup, with durable account/consent/effect checks at the owner.
It never retries an uncertain mutation to discover its outcome.

Workspace discovery needs host access to the exact first-party local origin and
the configured hosted browser origin. The browser manifest expresses host patterns;
the coordinator additionally enforces exact port, path, fragment, current document
and owning tab before reading local state or accepting commands. It receives no
broad tabs/history permission. Hosted documents may request registered navigation,
but cannot read local state or command capture. Release builds read the same
release-owned customer configuration as Brain; an empty development configuration
supports Preview and cannot produce a release package. The provisioning workspace
path is exactly `/provisioning` without a trailing slash.

Use push channels, not periodic onboarding-status requests. Bounded retry, liveness,
expiry timers, and wake reconciliation remain permitted. Target active local updates
at p95 <=250 ms and p99 <=1 s after commit, with healthy convergence <=1 s. These are
production qualification targets; unit tests do not establish these timings.

## Authentication and continuation

The setup UI session lasts at most 30 minutes. Claims, WebAuthn challenges, handoffs,
and hosted fresh-auth evidence retain their own shorter expiries. A controlled Brain
restart retains the non-authorizing journey and completed progress, but does not extend
authorization. New local credentials require current provisioning authority.

First enrollment consumes the context and challenge, registers the credential, and
creates the local session atomically. The context is bound server-side to the browser,
installation, intended creator, current grants, and user verification. Returning users
use normal local authentication. A repeated finish request cannot mint another session;
an ambiguous response uses reconciliation and fresh proof where needed. Registration
does not replace hosted identity. Local session cookies stay HttpOnly; only the local
CSRF value appears in the registration result body. Neither travels via a URL or hosted
service.

### Native entry and receiving setup codes

The prohibition on credentials in navigation applies to the registered workspace
and all browser-to-browser handoffs. The existing native launcher has one narrow,
local bootstrap exception before workspace admission: its single-use entry code
goes only to the fixed loopback bootstrap route. Runtime entry defaults to 30 seconds
and is capped at 120 seconds; provisioning entry is capped at 300 seconds. Consumption
immediately redirects to a credential-free local page. These responses use no-store
and no-referrer; production local-server access logs are disabled. The native launcher
checks the loopback process image and user, bounds the code, and constructs the fixed
origin itself. This code never reaches hosted setup, the extension coordinator, a
workspace record, or a return URL. This bootstrap exception does not apply to
browser-to-browser handoffs.

A live authenticated local subscription suppresses another launcher-created tab.
Its focus event asks an installed extension to focus the admitted workspace. Without
the extension, browser scripting cannot reliably bring the existing tab or browser
window to the foreground, including a local tab with a live subscription. The native
app gives factual feedback for that known-open local workspace while suppressing a
duplicate. It also cannot reliably locate an arbitrary browser tab while that tab is
visiting hosted setup. If no live local subscription proves the workspace
is reachable, the native recovery action asks whether to open setup; it does not
claim that a browser tab is open or that the app connection succeeded.

### App-link return

An explicit extension button may dispatch only
`ofca://onboarding?journey=<UUID>`. Before dispatch, the admitted extension
document arms one non-authorizing, ten-minute return intent in session storage.
It binds the journey, creator/disclosure draft scope, owning tab and current
document. The shorter twelve-second UI timer only offers manual-launch help;
it does not expire valid return intent or claim the app is absent.

The native launcher retains its existing local bootstrap exchange. After that
exchange, a temporary local return document at the exact
`/provisioning/native-return#journey=<UUID>` asks to navigate the original
workspace to the registered provisioning or Bridge route. The local document
first reads the exact owner endpoint. An authenticated matching snapshot can
select its route; an explicit authentication-required response can select the
normal local sign-in route. The latter is a navigation hint, never authenticated
readiness or a substitute local session. Network failure, focus, HTTP reachability
and an open browser port cannot confirm launch. The destination authenticates
normally before using owner facts or authority.

The closed `ofca.workspace.launch-return.v1` request contains only the journey
UUID and the `provisioning` or `bridge` route. The extension admits this special
non-owner caller only at the exact local return path and current browser document,
with the still-current owning document and scoped explicit-launch intent. It
receives no bootstrap code, cookie, CSRF value or owner snapshot and does not
upgrade any readiness fact. A bounded receipt records return-in-progress before
navigation and reconciles an ambiguous result from actual workspace ownership;
it does not repeat navigation to discover the outcome.
The worker admits a dedicated extension runtime port bound to the existing setup
tab and document. A closed command carries only a nonce, current journey,
draft scope, exact expected source URL and registered route. That document synchronously checks its current
URL, lifecycle and scope before navigating itself. A queued command cannot navigate
a replacement user document. The worker subscribes to tab completion before dispatch
and confirms the exact destination and new document within ten seconds, without
polling, redispatch or new browser permissions. The authenticated desktop continuation
also navigates the current setup document synchronously.

Only absence of any live registered workspace permits ordinary native entry.
Absent, expired or mismatched intent alongside an existing workspace must not
create a competing wizard. A successful return allows the temporary document to
close itself; the extension never closes an arbitrary tab. Completion activates
the owner only while the returning native tab and its window are still foreground;
otherwise it only updates the owner's registered destination. A slow startup must
not steal focus from a later user task. Recovery never reloads or navigates an
OnlyFans tab.

Manual launch from the Start menu has no journey argument. Before binding a new
journey or issuing its session, the bare local native-return document may request
`ofca.workspace.launch-discover.v1` with no other fields. It receives only the
journey UUID of the sole live, current, explicitly armed pending intent. This
read neither consumes nor extends intent. The native side keeps its expiring,
single-use bootstrap proof entirely same-origin; discovery cannot create or
extend authority and cannot retarget an existing protected local journey.
For both unconfigured explicit URI and manual launch, the fixed `/provisioning/native-entry` consumes
the native code into a separate HttpOnly entry cookie, then immediately redirects
to the credential-free native-return document. Explicit URI entry retains its
server-bound journey in the fragment; manual entry remains bare, including after
discovery. Chrome may retain the original document URL in external-message sender
metadata after a history-only fragment change, so the bare callback may return
only its already discovered journey under the same current intent and owner guards.
The initial navigation never replaces an existing provisioning cookie; same-origin
selection observes that cookie even when the initial cross-site navigation omitted it.
The local entry/selection endpoints retain
the original bootstrap deadline (at most 300 seconds), use same-origin CSRF and
single-use selection, and establish the normal session only after selecting the
journey. A lost selection response uses read-only reconciliation. An existing
valid provisioning session is retained; a different selected journey is refused.
Configured runtime launch continues through normal local passkey authentication;
discovery and return do not mint a local user session.
An expired/mismatched intent beside a live workspace refuses ordinary entry.
An explicit recovery button may send `ofca.workspace.launch-focus.v1`, without
other fields, from the exact local return document. It only focuses the current
registered owner and returns a closed `focused` receipt; it never opens or
navigates a tab and never claims authenticated readiness.

### Renewing the provisioning workspace

A fresh, single-use native bootstrap from an explicit desktop launch may renew
the exact local setup context. This preserves one workspace while requiring
current native authority to renew expired browser authority. It is a separate
recovery path; it does not extend the ordinary ten-minute app-link intent or
relax its focus-only fallback.

Normal native selection remains exactly `{journey_id}`. Recovery selection is
exactly `{journey_id: prior, recover: true}`, protected by the fresh native-entry
cookie, exact origin, same-origin CSRF, targeted prior reference and original
bootstrap deadline. Its verified result is
`{state: "selected", journey_id: resolved, previous_journey_id: prior}`. A consumed
entry read returns that result only with the session issued or retained by the
selection. A missing cookie leaves an unconfirmed result; it cannot issue another
session. Extra fields, coercions and a different valid session are refused.

Brain retains bounded durable prior-to-recovery linkage. Known expired unstarted
context can become a fresh draft only through this native selection. Preparing
and prepare-unknown retain the original operation and request for reconciliation
or refuse; they cannot silently become unstarted work. Unknown and completing
retain the exact installation, operation, scope and original recovery deadline.
They never become fresh preparation or repeat completion. Missing legacy linkage
is refused; recovery never selects an unrelated latest receipt.

Recovery selection rechecks native authority, context and deadlines within its
transaction, invalidates expired sessions and records one resolved mapping.
Concurrent or repeated selection of one native entry cannot create another
session or draft. A later fresh native entry retains a valid session only when
the durable mapping proves it belongs to the requested prior context. Without
that cookie, the new native authority may issue a session for the same mapped
journey; it cannot create another successor or extend the recovery deadline.
Session lifetime is capped by its journey; cached sessions and UUIDs cannot
recreate expired work.

Recovery does not require the extension. An unconsumed targeted native entry
exposes its launcher-bound `target_journey_id` to the same-origin callback. When
the extension is absent or confirms no workspace, the callback may request exact
prior-context recovery only if its fragment matches that binding. Untargeted
entry cannot choose a latest receipt, and a generic extension messaging failure
does not prove absence. Normal extension-first selection remains unchanged.
Without a targeted app link or an extension-held prior reference, manual launch
cannot recover an uncertain receipt by selecting a different or latest context.

Without the extension, same-origin browser messages may locate a responding
setup document for that prior context. These messages carry only navigation
correlation. A selected receiver independently verifies the cookie-bound
prior-to-resolved result, rechecks its document and lifecycle, and retires old
operations before refreshing itself. The new document authenticates the selected
context before acknowledging the return. Multiple responders are refused.

No response before dispatch permits continuation in the callback; it does not
prove that dormant physical tabs are absent, and later offers are ignored. Once
navigation is dispatched, a lost acknowledgement permits only receipt reads,
never redispatch or callback adoption. This preserves one active authenticated
context and supports acknowledged tab reuse without browser permissions, status
polling, or an unsupported focus claim.

The extension uses a separate bounded recovery navigation intent tied to the
stored journey, current creator/disclosure draft scope and exact native callback
document. A live local setup owner also binds its tab and document. If the owner
has closed, the coordinator first excludes conflicting registered workspaces and
retains the same draft. Ambiguous ownership, changed scope, unrelated hosted or
runtime pages, and replacement callback documents are refused. The UUID and draft
remain navigation scope, never authentication, consent or approval.

With extension coordination, verified selection returns fresh local context to
the existing setup document in that same tab, or the callback becomes the sole
setup owner if the old tab is absent. Old operations are retired before the new document acts. Lost
selection replies use reads; lost navigation replies reconcile actual ownership
without repeating navigation. OnlyFans tabs are never navigated or reloaded.
Delayed recovery does not steal focus after the user switches away.

A subscription suppresses fresh launcher entry only while its current durable
session and journey are valid. Their expiries join the existing one-shot deadline
wakeup. Subscription presence and focus do not establish valid setup authority.

Receiving setup codes use a dedicated, expiring workflow key. They do not grant
creator identity, local authentication, consent, installation registration, pairing,
or commercial rights. The hosted receiver separately authenticates the user and
checks current organization/creator authority. Browser receivers sign through the
admitted extension workspace. Desktop receivers use same-tab, exact-path form posts
containing only the public request/challenge/proof and journey UUID. The local return
restores the original HttpOnly cookie context; an authenticated read and CSRF-protected
signing request must match the durable local request. A public entry cookie provides
no signing authority. The return to hosted setup similarly requires its independent
hosted session and CSRF check. No local cookie, CSRF value or private key is forwarded.
An unknown signing result is not automatically retried; recovery obtains fresh proof.

## Attachment and controls

Keep one compatible observer per document across mode changes and worker restarts.
Account/document/consent generations fence asynchronous work; revocation stops collection
immediately. A timeout does not prove a reload is needed. The new signer is requested
with explicit observe-only behavior, validates the existing document and signing rules,
and fails without reload. If needed, one extension-owned inactive helper receives only
its authorized initial OnlyFans navigation, after observation has been registered.
Recovery has one operation owner shared with observer attachment. Existing user tabs
are never navigated. A deliberately closed helper is not recreated without a request.

The signer integration explicitly sets `captureMode: 'observe-only'` in
the shared signer owner used by both Agent runtimes. The packaged wrapper enforces
that setting after caller options, so compatibility defaults cannot restore reload.
Silent-document failure and cancellation remain failures until valid observation;
they never retry in the legacy mode. Shared observer/helper orchestration owns
attachment and recovery; consent changes cannot initiate a platform-tab reload.

Preview works permanently without Brain. Its persistent extension page owns pause and
resume. Full mode uses Bridge while the authenticated desktop connection is reachable;
the extension owns the controls while it is unreachable. Other views show confirmed
state and where the control is. A click cannot optimistically claim a completed pause.
The old toolbar popup is not the new onboarding or control surface.

## Compatibility and rollout

All new profiles have explicit version identifiers and are negotiated before use.
Existing v1 desktop-port and v2 runtime documents retain their bytes and meanings.
Clients lacking a capability show the existing supported route; they must not claim
automated continuation, silently widen authority, or introduce a reload fallback.
Contract publication and reviewed immutable consumer pins precede dependent services
and clients. Rollback disables new automatic starts and preserves completed authority
and recoverable progress. Prototype runtime code is not shipped in the product.

## Verification and limits

Contract fixtures, the projection reference reducer and packaged workspace fixtures
verify their stated boundaries. They do not establish deployed hosted behavior,
installer behavior, real passkeys or live platform compatibility.

Release requires architecture review, published contracts and pins, signer
qualification, real packaged Windows/browser journeys, ingress streaming,
consented read-only platform qualification, copy/accessibility review and measured
synchronization latency.
