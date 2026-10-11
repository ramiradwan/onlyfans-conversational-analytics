<!-- CODE-VERIFY: Check native-entry routing, expiry and return flow against app/provisioning/session.py, app/provisioning/native_return.py, app/provisioning/native-return.js, extension/runtime/onboarding-workspace.mjs and the native admission tests before editing. -->

# Diagnose desktop setup return

The desktop app receives the browser's setup request through its local listener. It checks the request before opening or returning to the existing setup workspace.

A new installation may open initial setup. An enrolled installation must use its saved continuation. A missing, expired, retired, or conflicting continuation cannot be replaced with a new enrollment automatically.

## When setup cannot be opened

1. Check that **Conversation Analytics** is running and that its local listener owns port `17871`.
2. Check the failed return page's `#native-return-status` element for `data-failure-stage`. The value identifies where the return stopped, not why it stopped.
3. Return to the extension workspace. Use the action shown there after checking the current connection status.
4. If the return still fails, retain the stage and the failing route for diagnosis. Do not copy setup codes, URLs with credentials, cookies, account content, or request bodies into the report.

The possible stages are `entry`, `workspace-prepare`, `workspace-select`, `state-read`, and `workspace-return`.

## Access boundaries

The return page serves only packaged local JavaScript and styles. Its status does not establish an authenticated session, creator approval, or commercial activation.

Initial enrollment requires a valid local entry. Saved continuation, one-time entry consumption, and browser-to-desktop acknowledgement remain separate checks. A failed or uncertain protected action must not be retried automatically.

For installer validation, see [Windows acceptance](../installation-and-acceptance.md). For normal setup, see [Install on Windows](../install-windows.md).
