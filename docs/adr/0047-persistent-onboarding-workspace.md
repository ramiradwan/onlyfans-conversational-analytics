# ADR 0047: Keep onboarding in one persistent browser workspace

- Status: Accepted for implementation; phase 2 qualification gates still apply.
- Date: 2026-10-08
- Decision authority: approved revised onboarding plan and completed reference review.
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
choosing the next unmet step. The reference's sessionStorage is not production authority.

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

Use push channels, not periodic onboarding-status requests. Bounded retry, liveness,
expiry timers, and wake reconciliation remain permitted. Target active local updates
at p95 <=250 ms and p99 <=1 s after commit, with healthy convergence <=1 s. These are
phase 4/5 measurement gates, not guarantees established by phase 2 unit tests.

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

## Attachment and controls

Keep one compatible observer per document across mode changes and worker restarts.
Account/document/consent generations fence asynchronous work; revocation stops collection
immediately. A timeout does not prove a reload is needed. The new signer is requested
with explicit observe-only behavior, validates the existing document and signing rules,
and fails without reload. If needed, one extension-owned inactive helper receives only
its authorized initial OnlyFans navigation, after observation has been registered.
Recovery has one operation owner shared with observer attachment. Existing user tabs
are never navigated. A deliberately closed helper is not recreated without a request.

The phase 2 dependency adoption explicitly sets `captureMode: 'observe-only'` in
the shared signer owner used by both Agent runtimes. The packaged wrapper enforces
that setting after caller options, so compatibility defaults cannot restore reload.
Silent-document failure and cancellation remain failures until valid observation;
they never retry in the legacy mode. The unified observer/helper orchestration and
removal of the old consent-controller reload routes remain phase 3 integration work.

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
and recoverable progress. The designer's dynamic template runtime is never shipped.

## Verification and limits

Phase 2 ships executable contract fixtures, a projection reference reducer, an isolated
packaged workspace feasibility slice, independently safe presentation corrections,
and a qualified signer candidate. Its test fixtures are not evidence of a live hosted
deployment, real installer, real passkey, or live platform compatibility.

Dependent phase 3 integration remains gated by the phase 2 evidence ledger, architecture
review, published contracts and pins, and signer qualification. The final release also
requires real packaged Windows/browser journeys, ingress streaming, consented read-only
platform qualification, copy/accessibility review, and measured synchronization latency.
