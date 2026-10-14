<!-- CODE-VERIFY: Check native-entry routing, expiry and return flow against app/provisioning/session.py, app/provisioning/native_return.py, app/provisioning/native-return.js, extension/runtime/onboarding-workspace.mjs and the native admission tests before editing. -->

# Diagnose desktop setup return

The desktop app receives the browser's setup request through its local listener. It checks the request before opening or returning to the existing setup workspace.

A new installation may open initial setup. An enrolled installation must use its saved continuation. A missing, expired, retired, or conflicting continuation cannot be replaced with a new enrollment automatically.

## When setup cannot be opened

1. Check that **Conversation Analytics** is running and that its local listener owns port `17871`.
2. Check the failed return page's `#native-return-status` element for `data-failure-stage`. The value identifies where the return stopped, not why it stopped.
3. If the return page offers **Check setup**, it can check the recorded outcome without repeating the protected selection. **Open setup tab** focuses an independently verified current setup tab.
4. If no action is offered, retain the stage and the failing route for diagnosis. Do not copy setup codes, URLs with credentials, cookies, account content, or request bodies into the report. Do not close a conflicting tab or clear stored setup data to force continuation.

The possible stages are `entry`, `workspace-prepare`, `workspace-reconcile`, `workspace-select`, `state-read`, and `workspace-return`.

## Access boundaries

The return page serves only packaged local JavaScript and styles. Its status does not establish an authenticated session, creator approval, or commercial activation.

Initial enrollment requires a valid local entry. Saved continuation, one-time entry consumption, and browser-to-desktop acknowledgement remain separate checks. A failed or uncertain protected action must not be retried automatically.

Saved-workspace recovery may reuse a tab only when authenticated evidence proves that it belongs to the saved target or has no current authority or unresolved accepted work. Replacement establishes a new presentation generation; an older page cannot inherit that authority from the browser's shared cookie. The destination verifies its selected context before the extension commits ownership and permits normal continuation. See [saved workspace presentation](../adr/0049-reconcile-saved-workspace-presentation.md).

For installer validation, see [Windows acceptance](../installation-and-acceptance.md). For normal setup, see [Install on Windows](../install-windows.md).
