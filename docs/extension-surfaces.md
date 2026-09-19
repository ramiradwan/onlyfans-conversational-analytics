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

## Design details

Preview is a complete two-milestone path. Its popup offers **Review Full analytics**,
not a claim that setup remains unfinished. Full adds Connect and Activate milestones.
The mock's checked legal checkbox is not a default: actual acceptance records decide
which checkboxes are checked. Required disclosure text and instrument links remain.

The popup's Clear link opens the data section in Options. Connection details also
open Options, not a popup subview. Disconnect, revocation, and deletion require a scoped confirmation.
Desktop-stored messages are never deleted by the extension's data action.

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

## Architecture impact

Architecture rationale:
Move multi-step work into extension-owned tabs. Keep capture, legal, pairing, and commercial authority in their existing controllers. Keep Bridge separate.

Potentially affected invariant dispositions:

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
