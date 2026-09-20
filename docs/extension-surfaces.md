# Extension surfaces

Design reference: **Extension Surfaces Refactor.dc.html**, supplied for this refactor.
Initial implementation base: `a97b311a4ca351d85182a2af91649e414fa98da5`.
Integrated upstream recovery fix: `e70f634a1cbf4cbccae3e895978bea8e28572da2`.

## Surface ownership

| Surface | Responsibilities |
| --- | --- |
| `extension/popup.html` | Status, Preview counts, Pause/Resume, and contextual links. |
| `extension/setup.html` | Legal review, mode choice, browser access, extension-side pairing, and continuation. |
| `extension/options.html` | Extension access, connection details, local data controls, and links to desktop settings. |
| Bridge | Existing desktop pairing confirmation and desktop-owned settings. Only handoff instructions change. |

Setup and Options are packaged extension pages. They do not import Bridge views,
stores, sessions, or API clients. They share the existing generated static tokens,
fonts, and extension presentation helpers. No permissions, external messaging
origins, or network policy are added to make the full pages work.

## State and commands

`extension/runtime/ui-surfaces.mjs` admits only named top-level extension documents.
Setup owns legal acceptance and pairing requests. Options owns deletion, revocation,
and forgetting a connection. The popup may pause or resume but cannot accept terms,
choose Full, delete data, or start a pairing attempt.

The existing consent and legal controllers still authorize transitions. The existing
companion client owns pairing and readiness. Pages submit actions and render those
results; a remembered review position is never proof of consent or completed setup.

Opening setup focuses an existing setup tab rather than starting another ceremony.
Pairing begins only after an explicit action. The initiating port owns the attempt:
closing an observing popup or switching apps does not cancel it; closing the owning
setup page does. Only its initiating port can issue the pairing Cancel command. Only setup receives the
comparison code, and confirmation remains in Bridge.

A connected desktop does not imply active commercial authority or admitted analysis.
Readiness is requested separately through the existing authenticated companion path.
A stale, malformed, or unavailable response cannot produce a ready state.
The paired Full popup shows desktop availability, secure connection, Full activation,
and analysis readiness separately; connection loss clears its ready indication.

## Design details

Preview is a complete two-milestone path. Its popup offers **Review Full analytics**,
not a claim that setup remains unfinished. Full adds Connect and Activate milestones.
The mock's checked legal checkbox is not a default: actual acceptance records decide
which checkboxes are checked. Required disclosure text and instrument links remain.

The popup's Clear link opens the data section in Options. Connection details also
open Options, not a popup subview. Disconnect, revocation, and deletion require a scoped confirmation.
Desktop-stored messages are never deleted by the extension's data action.
During comparison, opening the desktop app is the primary action and Cancel remains secondary.

Loading keeps the heading and a status message visible. Lower-page navigation appears
only once its position is known. Settings rows without an available action are hidden.
Light and dark themes, narrow windows, keyboard focus, and reduced motion use the
existing static design system. No new palette or Bridge application shell is introduced.

The state and transition inventory is in `docs/ux-journey.json`.

## Qualification

After installing each package's pinned dependencies:

```sh
npm test --prefix extension
npm test --prefix frontend
npm run test:browser:surfaces --prefix extension
npm test --prefix tools/e2e-capture -- tests/popup-lifecycle.spec.mjs
node --test tools/visual-capture/*.test.mjs
node tools/visual-capture/capture.mjs artifacts/visual-capture
python tools/validate_ux_journey.py
python tools/validate_architecture_boundaries.py
npm run build --prefix extension
npm run audit --prefix extension
```

The surface suite renders the actual page scripts against synthetic runtime states.
The lifecycle suite opens the actual toolbar action and uses the production companion
client with an isolated loopback peer. Release qualification exercises the built
extension's real legal controllers and deletion/reacceptance flow. These are distinct
from a full production Windows/desktop/hosted end-to-end qualification.

The visual workflow runs surface interactions using its cached Chromium executable,
then captures actual extension, provisioning, and Bridge pages. The existing Windows
E2E suite keeps its consent, native permission, capture, and desktop-pairing checks;
its helpers now follow setup/Options handoffs instead of retired popup controls.

For a live Full-mode source test, build with both `--legal-release-bindings=<path>`
and `--packaged-signing-rule=<reviewed-rule.json>`. A plain development build omits
the reviewed rule and deliberately refuses history acquisition. The pinned rule
and its verification coordinates are in `extension/qualification/signer-0.2.0.md`.
Customer entry links also require the release-owned `app/core/customer-release.json`
configuration; the source template has empty hosted URLs.

When attaching Playwright to the dedicated debug profile, use
`chromium.connectOverCDP('http://127.0.0.1:9222', { noDefaults: true })`.
Default focus emulation makes background pages report themselves as visible and
correctly trips the signer's safe-refresh refusal. Keep the real creator tab
inactive and draft-free while verifying the permitted cold-bootstrap refresh;
do not override the page's visibility or bypass the safeguard.

### Automatic recovery limits

Navigation and transient failures of a platform identity read clear capture
authority for that document; they do not assert sign-out or tear down the
authenticated companion. Explicit authentication refusal and a confirmed creator
switch still invalidate the companion. New capture waits for fresh identity.

Connection recovery uses exponential delays with jitter. Six unsuccessful or
short-lived attempts trigger a five-minute cooldown. UI polling, alarms and worker
reconstruction cannot skip that cooldown: the extension reserves each protected
handshake in `chrome.storage.local` before networking. That record contains only a
schema version, attempt count and next-attempt time. A connection must remain
usable for one minute before resetting the retry budget.

History cancellation on session loss also backs off (3 seconds, doubling to a
60-second cap). A disabled wake cannot erase the failure streak. Independently,
the signer may automatically refresh a platform tab at most once per 15 minutes
and three times per rolling hour. The encrypted account partition records the
reservation before calling the browser, so an interrupted wake or worker/browser
restart cannot refund a possible refresh. Storage failure refuses the refresh.
These limits supplement the signer's existing inactive, unfrozen, draft-free
checks. Passive capture and previously saved data do not depend on a refresh.

The explicit setup reload also waits at most two seconds for the browser's
acknowledgement. A frozen background document cannot hold the shared setup/status
queue indefinitely, and a late acknowledgement never schedules a second reload.

Recovery regression coverage includes wake storms, short sessions, cancelled
bootstrap, creator changes, delayed capture fencing, real encrypted IndexedDB
worker termination, and SPA navigation through the built MV3 extension.

## Architecture impact

Architecture rationale:
Move multi-step work into extension-owned tabs. Keep capture, legal, pairing, and commercial authority in their existing controllers. Keep Bridge separate.

Potentially affected invariant dispositions:

capture-control-separation:
not affected
rationale: Preview emits only passive read metadata and has no platform mutation channel. All capture controls stay in the consent controller.

consent-authorization:
not affected
rationale: The same trusted-sender, active-mode authorization and generation-checked capture scope admit Preview updates. Deletion still drains admitted writes before clearing local state. Legal disclosure text is unchanged.

checkpoint-monotonicity:
not affected
rationale: No checkpoint mutation or ordering changes.

durable-agent-delivery:
not affected
rationale: Capture and outbox ownership are unchanged.

release-integrity:
affected

replay-idempotency:
not affected
rationale: No ingestion or replay identity changes.

snapshot-integrity:
not affected
rationale: No snapshot storage or validation changes.

Invariant / boundary actually affected:
release-integrity: extension packaging includes the new documents and bundles. The Agent UI trust boundary admits named extension pages and assigns commands to their owning surface; existing runtime authority remains unchanged. Bridge changes are presentation-only handoff guidance.

Safety evidence:
Extension unit tests cover sender restrictions, pairing ownership, consent, legal evidence, permissions, and recovery. Browser tests exercise the real popup, persistent setup, comparison, confirmation, deletion, and reacceptance. Deterministic artifact audits, dependency boundaries, journey validation, accessible state captures, and frontend tests cover the pages. Native Windows and release-matrix checks remain CI obligations.

## Preview count correction

Preview now counts unique observed activity in today and the preceding six UTC
calendar days. Source message dates and last-message dates determine inclusion;
loading older data does not make it recent. The popup says Active chats and explains
that unopened history may be missing. Repeated reads, reloads and worker/browser
restarts do not add duplicate activity. Raw IDs are used transiently to derive local
HMAC tokens, and never retained in the Preview index. The bounded token index and
its key are cleared by the existing data controls. See ADR 0026 for retention,
capacity, account isolation and the amended Preview persistence boundary.

Approved legal disclosure copy is unchanged for review by the legal specialists.
For source qualification only, set `LOCAL_CUSTOMER_RELEASE_CONFIG` to an absolute
path containing a complete validated customer-release JSON document. Both the
secure setup link and hosted API origin come from that same document. Frozen
customer packages ignore this source override and use their bundled configuration.
