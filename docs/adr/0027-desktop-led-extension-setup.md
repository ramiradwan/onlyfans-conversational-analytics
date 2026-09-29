<!-- CODE-VERIFY: extension/runtime/desktop-port.mjs extension/runtime/ui-surfaces.mjs extension/runtime/companion-client.mjs extension/background.js extension/setup.js extension/ui/surface-client.mjs frontend/src/services/extensionPort.ts frontend/src/components/CompanionPairingControls.tsx app/security/companion_pairing.py app/api/endpoints/companion_pairing.py app/transport/manager.py app/persistence/auth_sql/0018_companion_confirmation_method.sql app/provisioning/provisioning.js app/api/endpoints/companion_session.py extension/transport/companion-channel.mjs extension/runtime/browser-surface.mjs extension/popup.js extension/options.js frontend/src/components/BrowserExtensionControls.tsx -->

# ADR 0027: Let the desktop app lead extension setup and controls with pushed state

- Status: Proposed
- Date: 2026-09-29
- Amends: [ADR 0024](0024-authenticated-companion-sessions.md), pairing steps 5 and 6 and its Bridge extension-message consequence; the surface ownership in [Extension surfaces](../extension-surfaces.md).

## Decision

When the desktop app is installed, the creator starts and finishes extension setup and pairing from Bridge, and manages the paired extension from Bridge. State reaches every surface by push, not polling. Extension-only use is unchanged.

### Desktop port

Bridge and the provisioning page open a browser port to the extension with `chrome.runtime.connect(EXTENSION_ID, { name: 'ofca.desktop' })`. The existing `externally_connectable` entry for `http://bridge.localhost:17871/*` already permits this, so the manifest and permissions do not change.

The extension admits a port only from that origin, from a top-level document (`frameId` 0) in a browser tab. The port carries no authority:

- The extension pushes a coarse stage whenever it changes: `unavailable`, `needs_terms`, `paused`, `needs_full`, `needs_site_access`, `needs_account`, `ready_to_pair`, `pairing`, or `paired`. It names no account, count, or identifier.
- `open` asks the extension to open setup or Options in its own compact window. Opens are rate-limited to one per second per port.
- `pair` and `cancel` start and cancel a pairing attempt. The port that starts an attempt owns it, as a setup page does. Closing the Bridge page cancels it. Only the owning port receives that attempt's comparison code.

Legal acceptance, mode choice, and browser permission prompts stay in top-level extension pages. Every state change still passes the consent, legal, and companion controllers.

### Browser-verified pairing

When the port is connected, Bridge opens Brain's pairing window first and then sends `pair`. The extension reports its comparison code over the port. Bridge confirms with `agent_comparison_code`, and Brain compares it with its own code in constant time.

A match commits the pin with `companion_confirmation_method = 'browser_verified'`. A mismatch declines the window and commits nothing. The operator's single Connect click is the confirmation. Without a port, for example when Bridge is open in another browser, the operator compares codes by eye as before and the pin records `operator_compared`.

### Pushed state

- Brain sends `companion.state` on the Bridge WebSocket at bind and after every pairing transition, including window expiry. It is a change notice with a revision and time. Bridge re-reads details through the authenticated pairing endpoints, which keep their per-session scope.
- The extension worker tells open extension pages to re-read when the companion session opens or closes, permissions change, the detected account changes, or delivery progresses. The pages no longer refresh on a three-second interval.
- An open desktop port counts as proof that the desktop app is running. Otherwise a page probes once per refresh, and refreshes are event-driven.

### Controls after pairing

Bridge owns pause, resume, and disconnect while it can reach the extension. Each action has one control:

| Action | Extension only, or desktop unreachable | Desktop can reach the extension |
| --- | --- | --- |
| Pause and resume | Popup | Bridge. The popup shows status and "Resume in the desktop app". |
| Disconnect | Options, Forget | Bridge, Disconnect. It removes both pins. |
| Site access and message history permission | Setup and Options | Bridge shows status and opens the one extension page with the browser prompt. |

Options no longer has its own Pause row; the popup owns it for extension-only use.

Browser state and controls use the companion session, so they work when Bridge is open in another browser:

- **Browser state.** The extension sends the RPC `agent.surface.report` with `capture` (`active`, `paused`, `off`), `site_access` (`granted`, `needs_approval`, `reload_required`), `history_permission` (`granted`, `missing`), and `legal_review_required`. It sends it when a session opens and when its state changes. Brain accepts it only after Agent authentication for the pinned account and keeps it in memory. `agent.state` carries it as `browser`, or null while no session is open.
- **Controls.** Brain sends the session document `session.control` with an `id` and an `action`: `capture.pause`, `capture.resume`, or `companion.revoked`. The creator requests pause and resume through `POST /api/v1/companion/browser/capture`; operators cannot. The extension applies pause and resume through the consent controller, so resume still requires Legal mode-choice evidence. Bridge shows the result only from the next `agent.state`.
- **Paused sessions.** Pause suspends the ingestion runtime. While paused from Full, the extension keeps a control-only session: it authenticates the pinned identity, reports browser state, and accepts controls. It opens no ingestion lease, unlocks no storage, carries no activity, and reconnects under the existing recovery budget.
- **Disconnect.** Bridge's Disconnect makes Brain send `companion.revoked` to that pin's sessions just before it commits the revocation. The extension forgets its pin only if Brain then closes the session within three seconds, so a revocation that fails leaves both sides paired.

The popup and Options learn whether Bridge can reach the extension from the worker (`desktop_control`), and hide the controls Bridge owns only while that is true.

## Why

The creator previously had to move between Bridge and extension pages by hand, and each side polled for the other's state. Pairing required reading the same six-digit code from two windows.

The loopback socket cannot carry extension state before pairing: ADR 0024 admits only pairing and handshake traffic there, and Brain cannot yet authenticate the extension. The browser port can. Chrome routes it by extension ID, and the extension checks the page origin.

Browser verification keeps the comparison's purpose: the Agent that answered Brain's window is the extension in this browser. A code read from the extension through the port meets that test without the creator reading digits. Brain still makes the final comparison, so a faulty Bridge cannot skip it.

## Consequences

- A local process that serves the Bridge origin while Brain is stopped could open a port. It can learn the coarse stage, open extension pages, and start an attempt that fails at Brain's installation-key proof. It cannot accept terms, change mode, grant permissions, or learn another port's code.
- Pairing from the extension's own setup page remains for the cross-browser case. Setup hides its Pair button while a desktop port is connected, so one control owns each action.
- A newly installed extension that finds the desktop app running opens setup on the desktop-guided path. The page still asks for every choice.
- `auth.sqlite3` gains `agent_pairings.companion_confirmation_method` (migration 0018).
- A paused Full extension keeps a session to Brain. It carries only authentication, browser state, and controls.
- Bridge shows browser state only while an extension session is open. A closed browser shows as unknown, not as the last value.
- Controls are session-level documents, not protocol v2 `command.execute` actions. Platform commands keep their configuration policy, capabilities, and read-only-variant exclusion.
- Extension pages update on events. Values with no event source, such as a pending count while disconnected, update on the next event or when the page regains focus.

## Architecture impact

Affected:

- **protocol-compatibility**: protocol v2 gains the Brain-to-Bridge message `companion.state`, and `agent.state` gains the nullable `browser` field. Their golden fixtures are validated by the Python and Bridge suites; the Agent never receives either.
- **consent-authorization**: the desktop port can request pages and pairing attempts but not consent, legal, or permission changes. Session controls reach only the consent controller's pause and resume, with its Legal checks. A paused control session carries no capture. Evidence: `extension/tests/desktop-port.test.mjs`, `extension/tests/companion-control.test.mjs`, `tests/test_companion_browser_controls.py`.
- **release-integrity**: the packaged SQL catalog in `packaging/runtime-files.json` gains migration 0018; the extension package adds `runtime/desktop-port.mjs` with an unchanged manifest.

Not affected:

- **checkpoint-monotonicity**, **replay-idempotency**, **atomic-canonical-commit**, **snapshot-integrity**, **durable-agent-delivery**: ingestion, outbox, and canonical persistence are unchanged. The worker only observes acknowledgements to signal pages, and a control-only session opens no ingestion lease.
- **transport-analytics-separation**: the transport manager only broadcasts change notices and browser state and relays controls; it constructs no analytics.
- **provisioning-trust**: the provisioning page reads the extension stage for guidance only. Identity detection, claim validation, and association are unchanged.

## Related

- [ADR 0022: Keep activation Legal evidence append-only in the Extension](0022-append-only-legal-activation-evidence.md)
- [ADR 0024: Protect Full-mode communication with locally paired sessions](0024-authenticated-companion-sessions.md)
- [Companion pairing contract](../companion-pairing-contract.md)
- [Companion session transport](../companion-session-transport.md)
- [Extension surfaces](../extension-surfaces.md)
