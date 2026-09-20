# ADR 0026: Deduplicate seven-day Preview activity with local keyed tokens

- Status: accepted
- Date: 2026-09-19

## Context

Preview counted every response observation. Reloading an unchanged conversation
could double its message count. Chat list pagination and refreshes similarly
inflated chat counts. Grouping by the read date also counted old activity as recent.

## Decision

Preview keeps a locally generated 256-bit HMAC key and keyed SHA-256 tokens in
trusted-context extension local storage. The token input is an unambiguous tuple
of record kind, creator identity, conversation identity and, for messages, message
identity. Account tokens partition the counts. The popup reports the most recently
observed account, never a sum of different accounts.

The page bridge transmits only the transient identifiers needed to derive tokens,
source activity time and message direction. It never transmits message text for
Preview. Raw identifiers, source URLs and exact timestamps are not written to the
Preview store or its diagnostics. The key and token records commit together through
the existing serialized capture scope. Worker and browser restarts retain the same
deduplication state. The key stays in the extension; it is not exposed to page code.

The window is today and the preceding six UTC calendar days. Messages use their
source sent time. Active chats use observed message activity or the chat's latest
message time, falling back to an explicit source update time only when necessary.
Unknown dates are not replaced with the time of observation. Old reads do not
reintroduce expired activity. Direction corrections replace an existing entry.

Token records store only UTC day and direction/kind. Entries expire with the
activity window. Clear removes the key and all tokens; a later read creates a fresh
key. Local-data deletion retains its existing capture drain and storage-clear
barrier. Legacy additive totals cannot be reconstructed and are removed on upgrade.

The index is bounded to 20,000 entries across accounts. It does not evict still-live
tokens and then recount them. If full, it rejects new entries and displays incomplete
coverage. Preview always describes its normal coverage as loaded activity, because
it cannot establish completeness of unopened history.

This amends the Preview counts-only storage paragraph of ADR 0021. Full-mode
encrypted ingestion and its protocol remain unchanged. This is an engineering
storage decision; disclosure wording and legal classification remain with the legal
specialists. Existing approved disclosure copy is unchanged in this implementation.

## Verification

Unit tests cover replay, source-date boundaries, direction correction, account and
record namespaces, concurrent admission, storage failure, restart, expiry, deletion,
legacy migration, bounded storage and absence of raw identifiers in stored state.
Browser tests reread and reload the same fixture through real capture scripts. Live
Edge verification compares aggregate results with the developer account's observed
responses without retaining message content or raw identifiers in artifacts.
