# ADR 0051: Keep account and activation decisions in the desktop

- Status: Accepted.
- Decision authority: Product owner's approval of native Connect and Full analytics decisions, direct identity-provider sign-in, popup-close cancellation, and an optional activation-code fallback.
- Supersedes: ADR 0050's hosted presentation of Connect and commercial confirmation. Its native admission, browser-task, durable ownership and offline capability requirements remain authoritative.

## Decision

The desktop displays the exact account connection or Full analytics offer and
records an explicit customer command. Brain reads and submits that decision to
Provider through the separately purpose-bound installation-key proof contract.
Provider retains customer, source-operation, offer and commercial authority.
An accepted decision is not proof of local readiness or a verified capability;
the existing provisioning and license-delivery owners establish those facts.

Chrome handles authentication. The desktop offers Google, Microsoft personal
accounts and the existing Entra fallback, including email one-time codes. It
shows “Finish signing in in Chrome.” while authentication is pending. No display
name form is added. An appropriate current customer admission skips selection.
The release configuration declares supported provider choices; unsupported
choices are refused rather than rendered as working routes.

Brain persists the closed provider choice and a distinct authentication child
identifier in the accepted command before contacting Provider. A changed choice
cannot mutate an accepted attempt. Retry and account switching use explicit
commands tied to the same commercial operation and journey. A
historically admitted attempt is distinct from a currently valid customer
session. Unknown outcomes remain unresolved until Provider reconciles them.

Provider owns Entra routing and OIDC validation. Neither the renderer nor Brain
constructs an authorization URL. The extension opens only the fixed Provider
ceremony entry and keeps the exact Chromium callback, ceremony and attempt
checks. The provider button starts the flow immediately. The normal waiting
state has no additional Open Chrome or Cancel sign-in buttons.

Chrome's exact `WINDOW_CLOSED` rejection records a bounded local `closed`
observation. It is not a Provider callback or proof of cancellation. As a narrow
exception to ADR 0050's page-closure rule, the admitted task may withdraw its own
authentication child through the authentication owner's `close_for_task` port.
Both helper and paired transports derive the child from saved task authority;
neither accepts operation, provider or child identifiers from the browser.
The owner persists the intent before network access, reads Provider, and sends
an idempotent cancel only for that same current, selected, cancellable child
before its original deadline. Recovery retains the exact parent, child and key
after document loss or restart. Provider linearizes cancellation against a
successful callback; admitted work and successor children are never cancelled
by an old closure. Other failures remain unknown. A selected closed task never
reopens. The only exception is Provider-confirmed admission with permission to
recover the same unfinished activation preparation. Brain emits the optional
Product-local `preparation_recovery: true` flag only for that exact live child.
A fresh explicit native retry rotates task admission; the extension reopens
only when its generation differs from the journalled closure. Reads and replay
of the closed generation cannot reopen it. The Provider entry URL, immutable
binding and original deadline remain unchanged; this flag grants no authority.
Closure records expire at the original task/operation deadline and are bounded
to 128, independently of retained domain outcomes.

After Brain acknowledges the durable closure observation, the helper finishes
through its existing exact-document handshake. It does not wait for polling to
observe Provider cancellation or label the parent operation complete. A lost or
refused acknowledgement keeps the helper's outcome unconfirmed. The owner
continues reconciliation after the helper closes; pairing still requires its
separate durable pin and completion receipt.

Authoritative Provider reads drive desktop progress. This withdrawal capability
grants no selection, switching, consent, parent-operation cancellation, native
command or WebView navigation authority.

Each authentication child has its own local browser task. Replacing the child
fences the previous task without replacing the underlying ceremony or changing
its original expiry. Restart recovery reads the exact accepted child and never
silently selects a provider again. Provider may authorize reopening an admitted
child only to recover preparation of the same unfinished activation; the desktop
uses the same browser operation and presents an explicit retry.

Decision projections contain only bounded display data and opaque correlation.
The accepted command captures the displayed reference and digest. A lost submit
reply or restart reads the original operation; it does not repeat consent.
Changed source admission invalidates the old display, even if the same customer
later returns. Exact accepted retries retain their original meaning.

Full analytics remains optional after local setup. The temporary pilot path
uses completed setup milestones followed by explicit free activation. Provider
does not receive message counts or other analytics. Eligibility and funding do
not themselves enable Full analytics: Brain must receive and verify the signed
capability through the existing delivery owner.

An optional, collapsed “Use an activation code” control accepts a code issued
elsewhere through that same delivery owner. It never reads the clipboard without
a customer action. A setup code and an activation code remain separate inputs
with separate authorities. The code path does not create pilot eligibility or
weaken signature, installation, key, capability or version checks. Delivered
capabilities retain their existing offline verification behavior.

## Verification

Cover immutable provider choice, duplicate commands, lost replies and restart,
cancelled and expired children, current-session reuse, explicit account switch,
source replacement, exact callbacks and worker restart. Verify noncreating
reads and separate consent, delivery and readiness states. Exercise keyboard
navigation, minimum window size and both themes, then qualify the matching
installed desktop, extension and Provider build in the Windows VM.
