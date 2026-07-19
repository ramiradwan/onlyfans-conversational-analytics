# Full-Stack Communication Specification

**Audience:** Project team and code-generation tools  
**Status:** Secondary specification aligned to accepted ADRs 0001–0010

## Normative authority

The accepted architecture decision records in [`docs/adr/`](docs/adr/README.md) are normative for Agent–Brain–Bridge communication. In particular, [ADR 0006](docs/adr/0006-canonical-communication-matrix.md) is the canonical operation matrix. If this document, the frontend design specification, a README, generated documentation, or an implementation note differs from an accepted ADR, the ADR governs and the secondary document must be corrected.

This document is a non-authoritative restatement for implementation readers. It does not supersede an ADR.

## Architecture

- Agent is the sole raw-ingestion producer. It durably sends account-partitioned snapshots and deltas to Brain; Bridge never reads Agent storage or proxies ingestion or commands ([ADR 0001](docs/adr/0001-agent-owned-raw-ingestion.md)).
- Agent observes platform-user presence, but Brain derives and publishes expiring `presence.state` and Agent connectivity. Bridge derives only its own Brain-socket connectivity locally ([ADR 0002](docs/adr/0002-brain-derived-presence.md)).
- `/ws/agent` and `/ws/bridge` fix the socket role. A role-specific hello binds one authorized `creator_account_id`; identity is immutable for the connection and Agent writes are fenced ([ADR 0003](docs/adr/0003-immutable-socket-identity.md)).
- Agent delivery is at least once through a durable outbox and Brain checkpoint. Bridge consumes a revisioned read model and explicitly resynchronizes gaps ([ADR 0004](docs/adr/0004-durable-reconnect-resync.md)).
- Brain serves immutable Agent configuration through an authenticated HTTP endpoint and uses WebSocket messages only to signal and report configuration revision state ([ADR 0005](docs/adr/0005-agent-configuration-versioning.md)).
- Protocol v2 retains role-and-direction-specific message unions and permanently excludes Bridge-originated command requests ([ADRs 0006](docs/adr/0006-canonical-communication-matrix.md) and [0010](docs/adr/0010-signer-history-acquisition-and-bounded-state.md)).
- The static ticket in ADR 0007 is a non-secret fixture restricted to explicit local-development mode ([ADR 0007](docs/adr/0007-stub-auth-for-dev.md)).
- Hosted provisioning issues signed grants, while Brain performs local WebAuthn and Agent authentication and issues purpose-bound runtime tickets ([ADR 0008](docs/adr/0008-production-authentication.md)).
- One loopback-only Brain process serves Bridge and uses authoritative SQLite stores with rebuildable projections ([ADR 0009](docs/adr/0009-local-first-topology-and-persistence.md)).
- Signer-backed acquisition is one-page-at-a-time inside Agent, repair is bounded and multi-frame, canonical identity is account-level, and Bridge history is REST-paged ([ADR 0010](docs/adr/0010-signer-history-acquisition-and-bounded-state.md)).

## Contract rules

- WebSocket unions are separate for Agent-to-Brain, Brain-to-Agent, Bridge-to-Brain, and Brain-to-Bridge. HTTP configuration operations have separate request/response schemas.
- The only supported current protocol is `protocol_version: "2"`; the only supported current Agent configuration schema is `config_schema_version: "2"`. These versions remain independent of extension semver, signer semver/signing schema, IndexedDB version, and SQLite schemas.
- Every WebSocket message uses a common envelope with `type`, `protocol_version`, `message_id`, optional `correlation_id`, and a type-specific `payload`. Bound socket identity, not an envelope account claim, supplies routing authority.
- Only the sender named in the matrix may originate a type. Brain never echoes an Agent type to Bridge; it validates the input and emits a Brain-owned consumer type.
- Unknown, wrong-role, pre-handshake, conflicting-identity, or unsupported-version messages produce `protocol.error` when safe and close the socket when marked fatal. They are never generically republished.
- Durable Agent operations use acknowledgments and deduplication. Bridge state uses snapshot plus revision-gap recovery. Presence and heartbeats use expiry rather than replay.
- Brain sends `bridge.session`, then the initial `state.snapshot`, `presence.state`, `agent.state`, and `system.state` for the bound account.
- Bridge displays command state but never originates `command.request`, `command.execute`, or an equivalent command message, and it has no direct Agent data or command channel.

## Canonical communication matrix

The following 25 rows restate ADR 0006. ADR 0006 remains authoritative if this table differs.

| Message type or operation | Transport | Sender | Receiver | Payload essence | Failure behavior |
| --- | --- | --- | --- | --- | --- |
| `agent.hello` | WebSocket | Agent | Brain | Protocol/capabilities, `agent_installation_id`, requested `creator_account_id`, source stream/checkpoint, applied config revision | Must be first. Brain rejects unauthenticated, unauthorized, incompatible, or incomplete hello and closes; no ingest is accepted. |
| `agent.session` | WebSocket | Brain | Agent | Accepted `connection_id`, fencing token, bound account, durable checkpoint/resume action, required config revision, lease parameters | Without it Agent sends no domain messages. Loss causes reconnect and a new connection/fencing identity. |
| `bridge.hello` | WebSocket | Bridge | Brain | Protocol/capabilities, `bridge_session_id`, requested account, optional last view revision | Must be first. Brain rejects invalid identity/version and closes; Bridge clears account-scoped state. |
| `bridge.session` | WebSocket | Brain | Bridge | Accepted `connection_id`, bound account, protocol/server versions | Without it Bridge does not render account state. Loss causes reconnect and a new initial snapshot. |
| `agent.heartbeat` | WebSocket | Agent | Brain | Connection/fencing identity, current applied config revision, health summary | Best effort and not replayed. Missed lease transitions `agent.state` to stale/disconnected; it does not itself change platform-user presence or ingestion progress. |
| `sync.required` | WebSocket | Brain | Agent | Reason, expected source state, snapshot requirements | Agent pauses later deltas, builds/sends a consistent snapshot, and retries after reconnect if the notice is lost. |
| `ingest.snapshot` | WebSocket | Agent | Brain | Bounded `begin`, single-entity-kind `chunk`, and `commit` frames for chats, messages, tombstones, and coverage evidence | Brain durably stages begin/chunks without checkpoint movement. Commit validates and atomically merges account-level truth, replaces only stream membership, and advances through `through_seq`; absence never deletes canonical entities. |
| `ingest.delta` | WebSocket | Agent | Brain | Stable `event_id`, source stream/sequence, one typed raw change | Persisted in Agent outbox until acknowledged. Brain deduplicates and accepts only the next contiguous sequence; gap leads to rejection or `sync.required`. |
| `ingest.ack` | WebSocket | Brain | Agent | Highest contiguous committed source sequence and optional snapshot progress (`snapshot_id`, next chunk, committed) | Before snapshot commit the old source checkpoint remains authoritative. Agent resumes the requested chunk after reconnect and compacts only after the final committed acknowledgement. |
| `ingest.rejected` | WebSocket | Brain | Agent | Correlation/event identity, validation code, retryable flag, safe detail | Retryable items remain queued with backoff. Non-retryable items block contiguous progress until explicit repair/quarantine policy or resync; no silent skip. |
| `state.snapshot` | WebSocket | Brain | Bridge | Bounded conversation summaries with one preview, analytics, acquisition coverage, projection readiness, live freshness, and `view_revision`; no historical message arrays | Sent after every Bridge bind/resync or projection-generation activation. Bridge stays loading/degraded until valid; reconnect/resync on loss or invalid payload. |
| `state.delta` | WebSocket | Brain | Bridge | Next `view_revision` and an atomic typed change set for conversation/analytics state | Bridge ignores duplicates, applies only the next revision, and sends `state.resync` on a gap or invalid change. |
| `state.resync` | WebSocket | Bridge | Brain | Last applied view revision and reason for recovery | Idempotent. Brain returns `state.snapshot`; Bridge does not claim realtime state while waiting. |
| `presence.observed` | WebSocket | Agent | Brain | Complete normalized online `platform_user_id` list, observation id/time | Ephemeral and never outbox-replayed. Invalid/out-of-order data is ignored/rejected; silence expires to unknown rather than offline. |
| `presence.state` | WebSocket | Brain | Bridge | Authoritative list, `current/unknown` freshness, server receipt/expiry and last-observation metadata | Bridge replaces the presence slice and marks it unknown at `expires_at`. A reconnect receives current state; stale data is never rendered as current. |
| `agent.state` | WebSocket | Brain | Bridge | `connected/stale/disconnected`, installation metadata, required/applied Agent-config revisions, required/applied history-settings revisions, degraded reason | Brain derives it from leases and account-scoped configuration state. Bridge never substitutes local extension detection; expiry yields stale/disconnected. |
| `system.state` | WebSocket | Brain | Bridge | Independent acquisition coverage, projection readiness, and live freshness with safe reasons and revisions | Last value is replaceable state. Bridge never collapses the dimensions into false completeness and receives a fresh value after binding. |
| `protocol.error` | WebSocket | Brain | Agent or Bridge | Error code, correlation/message id, retryability/fatal flag, safe detail | Fatal errors close after delivery attempt. Nonfatal errors leave the relevant checkpoint/revision unchanged; clients follow the indicated retry/resync action. |
| `agent.config.get` | Authenticated HTTP request (`GET /api/v1/agent/config`) | Agent | Brain | Authenticated context, current ETag/revision and supported config schema | Timeout/5xx keeps last known good config and retries. Unauthorized fails the Agent session; `304` reuses validated cached content. |
| `agent.config.document` | Authenticated HTTP response | Brain | Agent | Immutable config revision, schema version, digest/ETag, capture/command policy, and consent-bound history-acquisition limits | Agent rejects invalid/unsupported/digest-mismatched content, keeps last known good config, and reports degraded; it never partially applies or proactively reads without a matching session, consent, and signer identity. |
| `config.available` | WebSocket | Brain | Agent | Newly required config revision/digest | Signal is idempotent. Loss self-heals because every new `agent.session` repeats the required revision. |
| `config.applied` | WebSocket | Agent | Brain | Applied revision/digest, activation outcome, relevant capability status | Agent repeats applied revision in hello/heartbeat. Brain retains required/applied mismatch and exposes degraded `agent.state` until confirmed. |
| `command.execute` | WebSocket | Brain | Agent | `command_id`, bound account, allowed typed action, deadline, idempotency policy | Agent validates account, allow-list, deadline, and fencing before execution. Duplicate `command_id` returns stored result; Brain does not blindly retry a non-idempotent action without deduplication. |
| `command.result` | WebSocket | Agent | Brain | `command_id`, accepted/succeeded/failed status, safe result/error metadata | Agent persists terminal results until acknowledged. Brain deduplicates; timeout becomes an auditable unknown/failed command state, not a Bridge proxy fallback. |
| `command.result.ack` | WebSocket | Brain | Agent | `command_id` and recorded terminal result identity | Agent may compact the persisted result only after ack; duplicate acks are harmless. |

## Non-matrix implementation choices

Capture hooks, browser-internal events, state-store organization, schema-generation tooling, event-distribution technology, and UI component assignment are implementation choices. They must preserve the four role-and-direction-specific WebSocket unions, immutable Brain-authorized routing, the Agent-only ingestion and command boundary, durable acknowledgment rules, and Bridge revision recovery.

## History acquisition and repair contract

- `local-authenticated-read-connector` validates and executes one `identity`, `conversations`, or `message-page` read. Agent alone owns page loops and advances a continuation only after atomic `commitPage` success.
- The signer response contains typed items, opaque continuation, and `inventory_end` or `history_start` boundary evidence. It exposes no raw body and no completion claim.
- Coverage enters the normal contiguous delta stream as one of six typed `coverage.observed` variants. Brain freezes inventory membership and derives complete or partial outcome; Agent never sends `complete`.
- Snapshot frames are encoded below 512 KiB, target 448 KiB and 100 records, and reject a normalized record over 384 KiB. A chunk has exactly one `entity_kind`. No content, page, root, or chunk digest is transmitted; Brain establishes duplicate-chunk idempotency by exact normalized staged-row equality.
- Duplicate material and duplicate equal evidence are no-ops. Conflicting equal-version chats, conflicting material under one message ID, conflicting evidence, and chunk-index reuse with different staged rows are invariant failures.
- Chats and messages are canonical by account plus platform ID. Stream membership may be replaced on snapshot commit, but snapshot absence never deletes canonical truth and historical material never revives tombstones.

## Bridge REST surface

All routes obtain `creator_account_id` from the authenticated Brain session; clients cannot select it in query parameters.

```http
GET /api/v1/conversations/{conversation_id}/messages?before={opaque_cursor}&limit={1..100}
GET /api/v1/settings/history
PUT /api/v1/settings/history
DELETE /api/v1/settings/history/consent
```

Message pages default to 50, sort stably by `(sent_at, message_id)`, return oldest-to-newest, and bind their HMAC cursor to account, conversation, projection generation, timestamp, and message ID. Cross-scope cursors reject; obsolete projection generations return `409 cursor_stale`. `older_cursor: null` means no older locally stored row, not upstream completeness.

Settings mutations require creator authority, same-origin CSRF validation, and `If-Match` against the configuration revision. Brain persists desired state before publishing Agent configuration. Operators may view but not mutate. Revocation disables proactive reads; desired and effective state remain distinct until `config.applied` confirms the revision.

## Bridge truthfulness and boundedness

WebSocket state scales with conversation count, not message count. The frontend keeps bounded per-conversation REST pages, virtualizes message rendering, preserves the visible anchor when prepending, and follows live appends only when already near the bottom.

Acquisition coverage, projection readiness, and live freshness remain independent. “Up to date” requires complete applicable acquisition, projection current through the coverage close revision, current live freshness, and aligned desired/effective configuration. Partial metrics carry basis, observed range, complete range, sample size, as-of time, and projection revision; lifetime and trend claims require compatible complete coverage.
