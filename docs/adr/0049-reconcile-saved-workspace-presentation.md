# ADR 0049: Reconcile saved setup with its browser workspace

- Status: Accepted.
- Date: 2026-10-11.
- Decision authority: Product owner.
- Extends: ADRs 0047 and 0048.

## Decision

Qualify the desktop app's saved installation independently of the extension's
presentation record. A fresh native saved entry may reuse the exact sole local
provisioning document when the local server establishes that it belongs to the
selected saved target, or that it has no current authority or unresolved accepted
work. The latter is presentation-only supersession. It imports neither account
identity nor approval and clears obsolete scoped draft choices.

The server classifies the durable workspace locator and physical document locator
separately under the native entry's existing authority. Neither locator selects
an account. An unknown historical row, HTTP status, tab age or tab count cannot
establish the classification. Current contexts, accepted operations, workers,
transfers and uncertainty receipts retain their existing authority and lifetime
checks. Missing outcome evidence refuses supersession until an exact result read
or qualified retained receipt supplies it.

Use an explicit saved supersession selection, bound to the entry, inspected
locators, offered target and classification generation. Revalidate in the existing
single-use selection transaction. Preserve origin, CSRF, installation key, target
generation and replay boundaries. For a matching target, retain the installation
context and accepted work while retiring superseded browser presentation authority.
Establish a genuine presentation/session generation boundary; reusing a cookie and
CSRF value alone is insufficient.

Require the current journey and presentation generation before coordinating
state, status and event reads may cause effects, as well as before protected
commands. An older document seeing a replacement same-origin cookie cannot act
on the newly selected context. Recheck the original context after awaited work;
already accepted external requests remain inventoried for outcome reconciliation.
The native bootstrap and non-creating selection-result paths retain their separate
authority.

A cross-site return shell may check the existing authenticated cookie against its
exact journey through a read-only navigation context. This check exposes neither
presentation generation nor CSRF and coordinates no setup work. The subsequently
loaded authenticated document receives its generation and must qualify ownership
before continuing.

The extension inspects the exact top-level document without mutation, then
independently verifies the local selected result. It rechecks source document,
candidate document, registered URL, census, account and disclosure scope before
one navigation. The local server does not observe or authorize Chrome's tab census.
An already loaded inactive document need not implement a new retirement listener:
verified inactivity and server fencing permit exact-document replacement using
the browser lifecycle. Do not synthesize page events or invoke private handlers.

Store workspace, identity and bounded presentation transition in one versioned
canonical extension-storage envelope. Serialize every writer. Validate legacy
workspace and identity as a matched pair; do not invent missing values or fall
back to stale keys after a canonical envelope exists. Convert readers, writers
and observers together. Recovery rollback must retain the canonical-envelope
extension and generation-aware local surface, including owner and transition
receipts. Downgrading either component to a version that does not enforce these
boundaries is unsupported until separately qualified; deleting legacy keys alone
does not make such a downgrade safe.

Persist and read back the dispatch claim before navigation. Keep the original
deadline across restart. A claimed dispatch is reconciled by reads, never blindly
dispatched again. Fresh authority may retire an expired presentation transition
under a newer generation; independent protected uncertainty remains intact.

Verify the new destination document through authenticated read-only local state,
commit and read back its owner, then release public recovery readiness. Automatic
continuation must not start before this commit or wait for readiness that itself
depends on the commit. A temporary callback may close itself only after confirmed
return. No unrelated document or OnlyFans tab is reloaded or closed.

## Why

Browser navigation and extension storage do not share a transaction. Presentation
records can therefore disagree with a live document after interruption. Recovery
must reconcile them against local authority while preserving the user's completed
work and the single active workspace.

## Consequences

Normal verified recovery adds no user action. It shows “Continuing setup…” and
then the actual next unmet task. Completed consent, creator approval and the
installation key remain authoritative; UI drafts do not substitute for them.
Local passkey, pairing and commercial activation retain their independent checks.

A current different owner may be focused only after qualification. Multiple
documents, nonlocal conflicts, indeterminate authority and uncertain work do not
receive an arbitrary chooser or ineffective retry. Transient read failure may
offer a new read without replaying selection or another action. Cause-specific
copy requires a confirmed cause.

Ordinary native recovery, extension-first, no-extension and receiving-code routes
retain their existing protocols. This decision does not change Provider or
commercial authority.

## Related

- [ADR 0047: Persistent onboarding workspace](0047-persistent-onboarding-workspace.md)
- [ADR 0048: Continue saved installation setup](0048-continue-saved-installation-setup.md)
- [ADR 0008: Hosted provisioning and local authentication](0008-production-authentication.md)
- [Native setup return](../operations/native-setup-return.md)
