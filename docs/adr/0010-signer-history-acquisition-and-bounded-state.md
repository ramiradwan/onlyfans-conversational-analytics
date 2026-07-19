# ADR 0010: Integrate signer history acquisition with bounded account-scoped state

- Status: accepted
- Supersedes: the protocol-v1 and monolithic-snapshot compatibility statements in ADRs 0004, 0005, 0006, and 0009

## Context

Passive capture cannot recover conversation history that was not observed while the MV3 Agent was active. `local-authenticated-read-connector` can perform authenticated, read-only platform requests, but it must not become a scheduler, persistence layer, or second ingestion protocol. Full histories can also exceed worker memory, WebSocket frame, frontend store, and browser DOM limits.

The product therefore needs one account-safe acquisition path, scalable repair, deterministic canonical identity, truthful coverage semantics, and bounded Bridge state.

## Decision

Ship one zero-user coordinated schema replacement using protocol version `"2"`. Protocol, Agent configuration schema, extension release, signer package/signing-generation schema, IndexedDB schema, and SQLite schemas remain independently versioned. No v1 fallback, dual-write path, or legacy importer is added.

### Ownership

- The signer package validates and executes exactly one typed read page. It returns normalized items, an opaque continuation, and explicit boundary evidence.
- Agent owns consent/session/identity gates, cross-page scheduling, retry, cursor advancement, durable jobs, normalization, merging, deduplication, recovery, and raw-delta sequencing.
- Brain owns authorization, canonical persistence, deterministic conflict enforcement, coverage derivation, projections, message-page authorization, and Bridge state.
- Bridge consumes only Brain-owned summaries, revisions, paged history, settings, and status dimensions.

Raw response bodies are discarded immediately. Persistent Agent state contains normalized entities, tombstones, opaque upstream cursors, and safe boundary evidence only. Signing generations, cookies, signing headers, upstream cursors, and raw bodies never cross the Agent boundary.

### Agent account isolation and identity

Only `agent_installation_id` is installation-global. Each Brain-authorized creator account has a separately named IndexedDB database derived from a stable account hash, with the exact account identifier verified in its metadata. Stream/checkpoint state, applied configuration, command results, entities, tombstones, jobs, coverage, signer generations, outbox entries, and snapshot state are account-partitioned. Account epoch and job lease checks fence every asynchronous commit.

Canonical Brain identities are `(creator_account_id, chat_id)` and `(creator_account_id, message_id)`. Installation, stream, event, sequence, and acquisition origin are provenance and deduplication fields, never canonical keys.

### Deterministic merge

- Identical material is a no-op and allocates no new source transition.
- A full chat replaces a placeholder; a placeholder never replaces a full chat.
- For full chats, greater upstream `updated_at` wins; older material is a no-op; equal-version conflicting material and platform-identity conflicts are invariant failures.
- Message identity, parent, sender, text, sent time, and direction are immutable until an upstream edit-version contract exists. Conflicting material under one message ID is an invariant failure.
- Unknown deletes create tombstones, repeated deletes are no-ops, and historical observations cannot revive tombstones.
- Passive and signer origins never determine precedence.

Agent and Brain run the same merge-property fixture suite.

### Atomic page ingestion and coverage evidence

Agent advances an upstream continuation only after `commitPage` atomically stores normalized material transitions, sequenced evidence, the next opaque cursor, the durable job update, and outbox rows.

Coverage uses typed `coverage.observed` changes for `generation.started`, `inventory.member`, `inventory.ended`, `conversation.history_started`, `conversation.head_reconciled`, and `generation.closed`. No event contains a digest or trusted completion flag. Brain freezes membership at inventory end and derives complete coverage only when every frozen member has valid history-start and head-reconciliation evidence covering the generation `as_of`. Closing with missing evidence yields partial coverage. A newly discovered conversation invalidates current completeness and starts a new generation while retaining the prior `complete_as_of`.

### Bounded snapshot repair

`ingest.snapshot` remains the irrecoverable-gap repair operation but is a `begin | chunk | commit` frame union. Frames are at most 512 KiB; Agent targets 448 KiB and 100 records per chunk, rejects an individual normalized entity over 384 KiB, and orders chat, message, then coverage-evidence chunks. Begin and chunks are durably staged without checkpoint advancement. Commit validates all staged rows and atomically advances the source checkpoint. Duplicate equal chunks are no-ops; reuse of an index with different rows is rejected. Snapshot absence never deletes canonical account data.

Agent creates the as-of snapshot incrementally with a durable scan cursor and copy-on-write overrides, pauses delta transmission while retaining later deltas, and resumes build or send after termination. Brain performs staged validation and merge with set-based SQLite operations. Neither side materializes all history in one process object.

### Canonical and projection persistence

`canonical.sqlite3` owns raw events, stream epochs/fences/checkpoints, snapshot staging and receipts, stream membership, account-level canonical entities and tombstones, conflicts, coverage state, canonical revisions, durable projection work, activation intents, and Bridge revision allocation. `projections.sqlite3` owns active/building read-model generations, conversation summaries, message pages, analytics/NLP/LPG output, coverage presentation state, projection high-water marks, and the Bridge change log.

Snapshot commit changes canonical truth and appends one coarse projection-reseed item in one canonical transaction. Projection rows are account-scoped and stored in exactly two slots. Canonical activation identifies the visible generation, which deterministically resolves to one slot; every read-model query includes that slot. Projection work is replayed in bounded batches into the other slot while the activated slot remains readable. The second slot is seeded once with a set-based SQLite copy, subsequently catches up from its own projected revision, and is fully rebuilt only when a reseed intervenes. Committing the building slot does not expose it. Canonical activation switches visibility to the committed generation, after which the former active slot becomes the next building slot. A crash before activation therefore continues serving the prior valid generation, and restart completes the durable activation without a third slot. Each activation is followed by a fresh `state.snapshot`.

### Bounded Bridge state and settings

WebSocket state contains conversation summaries, one latest-message preview, analytics, acquisition coverage, projection readiness, and live freshness. It never contains complete historical message arrays. Historical messages always use authenticated REST paging:

`GET /api/v1/conversations/{conversation_id}/messages?before={opaque_cursor}&limit={1..100}`

The HMAC-authenticated cursor binds account, conversation, projection generation, timestamp, and message ID. Stale generations return `409 cursor_stale`; exhaustion of local rows does not imply upstream completeness.

History consent and desired state use `GET /api/v1/settings/history`, `PUT /api/v1/settings/history`, and `DELETE /api/v1/settings/history/consent`. Mutation requires creator authority, CSRF protection, and optimistic `If-Match`; operators are read-only. Desired and Agent-applied revisions remain distinct.

Bridge reports acquisition coverage, projection readiness, and live freshness independently. “Up to date” is possible only when acquisition is complete, projection covers the close revision, live freshness is current, and desired/effective configuration is aligned. Partial analytics state their synchronized-subset basis, range, sample size, as-of time, and projection revision; lifetime claims require complete applicable coverage.

## Security and extension packaging

The signer generation is bound to the expected platform creator identity. The extension adds observation-only `webRequest` and forbids `webRequestBlocking`, cookie APIs, debugger, native messaging, remote code, and unexpected origins. A lockfile-pinned deterministic build bundles the signer into the MV3 worker and audits the resulting artifact.

## Consequences

- Full history and repair remain bounded and restartable.
- Stream resets and multiple installations converge on one account-level canonical history.
- Coverage and analytics claims become evidence-derived and product-truthful.
- Bridge startup and WebSocket cost scale with conversation count rather than message count.
- The coordinated release intentionally resets development data and cannot interoperate with v1 peers.
- Live qualification still requires one explicitly consented platform account; deterministic tests cover all non-live Beta gates.

## Confirmation

Beta qualification requires cross-language v2 golden/invalid fixtures, merge permutations, atomic page fault injection, account-switch fencing, begin/chunk/commit restart tests, 10,000-message CI repair, 100,000-message bounded-memory qualification, exact fixture-to-render comparison, permission/remote-code audits, paging/cursor isolation tests, settings authorization/CSRF tests, accessibility checks, and one sanitized consented live multi-page read proving explicit boundaries and zero mutation requests.
