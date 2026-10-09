# Packaged workspace feasibility

`workspace-feasibility.mjs` bundles the real `runtime/onboarding-workspace.mjs`
coordinator with an explicit fixture adapter into a Manifest V3 extension. It
loads that package into real Chromium **after** a browser setup page is already
open. The production entry points and release manifest are intentionally not
changed by this architecture slice.

Run from the product repository, with extension dependencies installed:

```powershell
node --test extension/tests/onboarding-workspace.test.mjs
node extension/qualification/workspace-feasibility.mjs --report extension/qualification/evidence/workspace-feasibility.json
```

An isolated worktree can reuse installed dependencies read-only with
`--modules-dir <absolute-path-to-extension/node_modules>`. Use
`--browser-executable <path>` to select a compatible Chromium. The harness needs
the DevTools Extensions domain to install an unpacked package and trigger the
real toolbar action. This developer-only interface is not a product dependency.

## Observed evidence

The report records the browser version, source and bundle hashes, and eight
independent assertions:

1. A page loaded before installation has no external `chrome.runtime` API. The
   install continuation discovers the exact registered first-party path and
   canonical opaque journey UUID, then navigates only that setup tab to the
   extension page. The same tab target survives and the active user tab keeps
   focus.
2. Checkbox draft choices survive reload without creating any consent or
   authority fields.
3. Extension, hosted fixture, local provisioning, and Bridge routes reuse the
   same tab. A newly loaded allowed first-party page can use external messaging.
4. A real local HTTP event-stream server is stopped and restarted. Its epoch
   changes; the existing document reconnects with one event-stream request and
   no new tab. This is a **Brain transport fixture**, not a Windows executable
   restart or proof of production authentication recovery.
5. Chrome's `serviceworker-internals` Stop control actually stops the MV3 worker.
   The test observes `STOPPED` and proves that the next request starts a worker
   with a different nonce and the saved draft intact. Merely closing a DevTools
   target was insufficient evidence and is not used.
6. Repeated real toolbar actions focus the same workspace without resetting its
   current route.
7. Closing that tab and opening it again restores the draft in one new workspace.
8. An unrelated synthetic OnlyFans URL retains its document nonce and unsaved
   text, with zero navigation or reload events. The harness intercepts this URL
   locally and never contacts OnlyFans.

## Architecture consequences

The coordinator uses `storage` and narrowly registered first-party host access;
it does not need `tabs`, `activeTab`, or OnlyFans permission. Chrome can withhold
the URL even for an extension-owned tab in `tabs.get`; `runtime.getContexts`
supplies its current document identity. Web tab discovery needs first-party host
permissions. Exact path, port, and canonical UUID are checked again after the
broader origin query. Arbitrary return URLs, query credentials, and additional
navigation fields are rejected.

The registration is navigation metadata, never evidence of a running Brain,
creator identity, consent, or pairing. `read` and `resumeExisting` are worker
APIs; a production message adapter must admit callers before exposing data or
commands. `open` requires an explicit user action. The install continuation
does not focus a tab. Draft writes never move focus or navigate.

Draft scope has an opaque generation and disclosure bundle digest. Changing
the intended creator generates a new scope and clears Full analytics choice;
changing disclosures clears all checkbox choices. Stale scope writes and stale
packaged document identities are rejected. A checked draft cannot authorize a
controller action.

Before production integration, configure the real hosted route and permission
copy, connect the common journey owner to the Brain and hosted stores, and
qualify installed-app restart/authentication and browser process restore. Finish
extension consent and permissions before opening the existing pairing port:
navigating its document during live pairing still cancels its owned attempt.
No fixture result changes that ownership rule. The same-tab fallback refreshes
the setup document only; there is no fallback that reloads OnlyFans.

Chrome documentation:
[tab URL visibility and host permissions](https://developer.chrome.com/docs/extensions/reference/api/tabs),
[extension contexts](https://developer.chrome.com/docs/extensions/reference/api/runtime#method-getContexts),
[DevTools extension qualification methods](https://chromedevtools.github.io/devtools-protocol/tot/Extensions/).
