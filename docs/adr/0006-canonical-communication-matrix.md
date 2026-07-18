# ADR 0006: Adopt the canonical Agent-Brain-Bridge communication matrix

- Status: accepted

## Context and problem statement

The protocol needs one normative answer to “who may send this message, to whom, and what happens if it is lost or invalid?” Mixed unions and generic forwarding make routing semantics implicit and permit role-confused messages to reach handlers. Role-specific contracts make direction, ownership, and recovery behavior explicit.

## Decision drivers

- Exactly one originating subsystem for every message type.
- Role-confused traffic must fail at validation, not reach a generic forwarding branch.
- Delivery and recovery behavior must be explicit per message.
- Raw ingest, derived state, presence, configuration, and commands need distinct reliability semantics.
- The contract must support generated role-specific schemas and exhaustive handlers.

## Considered options

### Keep one inbound and one outbound union

This minimizes schema files and can share a generic envelope.

Trade-offs: allowed directions remain implicit, mixed recipients accept irrelevant types, and every handler needs runtime role checks.

### Define role-and-direction-specific unions plus one canonical matrix

Each socket has a fixed union and every type has one owner. A small common envelope remains shared.

Trade-off: there are more schemas and explicit conversion messages.

### Use a general event bus with sender and recipient fields

This is flexible and supports future actors without schema changes.

Trade-offs: routing authority moves into client-controlled payloads, exhaustive validation weakens, and ownership becomes convention rather than protocol.

## Decision outcome

Choose **role-and-direction-specific discriminated unions governed by the matrix below**.

### Contract rules

- WebSocket unions are separate for Agent-to-Brain, Brain-to-Agent, Bridge-to-Brain, and Brain-to-Bridge. HTTP configuration operations have separate request/response schemas.
- Every WebSocket message uses a common envelope with `type`, `protocol_version`, `message_id`, optional `correlation_id`, and a type-specific `payload`. Bound socket identity, not an envelope account claim, supplies routing authority.
- Only the sender named in the matrix may originate a type. Brain never “echoes” an Agent type to Bridge; it validates and emits a Brain-owned consumer type.
- Unknown, wrong-role, pre-handshake, conflicting-identity, or unsupported-version messages produce `protocol.error` when safe and close the socket when marked fatal. They are never generically republished.
- Durable Agent operations use acknowledgments and deduplication. Bridge state uses snapshot plus revision-gap recovery. Presence and heartbeats use expiry rather than replay.
- Brain sends `bridge.session`, then the initial `state.snapshot`, `presence.state`, `agent.state`, and `system.state` for the bound account. Subsequent streams retain their own stated revisions/freshness.
- Protocol v1 permanently excludes Bridge-originated command requests. Bridge displays command state but never originates `command.request`, `command.execute`, or any equivalent command message, and it has no direct Agent data or command channel.

## Canonical communication matrix

| Message type or operation | Transport | Sender | Receiver | Payload essence | Failure behavior |
| --- | --- | --- | --- | --- | --- |
| `agent.hello` | WebSocket | Agent | Brain | Protocol/capabilities, `agent_installation_id`, requested `creator_account_id`, source stream/checkpoint, applied config revision | Must be first. Brain rejects unauthenticated, unauthorized, incompatible, or incomplete hello and closes; no ingest is accepted. |
| `agent.session` | WebSocket | Brain | Agent | Accepted `connection_id`, fencing token, bound account, durable checkpoint/resume action, required config revision, lease parameters | Without it Agent sends no domain messages. Loss causes reconnect and a new connection/fencing identity. |
| `bridge.hello` | WebSocket | Bridge | Brain | Protocol/capabilities, `bridge_session_id`, requested account, optional last view revision | Must be first. Brain rejects invalid identity/version and closes; Bridge clears account-scoped state. |
| `bridge.session` | WebSocket | Brain | Bridge | Accepted `connection_id`, bound account, protocol/server versions | Without it Bridge does not render account state. Loss causes reconnect and a new initial snapshot. |
| `agent.heartbeat` | WebSocket | Agent | Brain | Connection/fencing identity, current applied config revision, health summary | Best effort and not replayed. Missed lease transitions `agent.state` to stale/disconnected; it does not itself change platform-user presence or ingestion progress. |
| `sync.required` | WebSocket | Brain | Agent | Reason, expected source state, snapshot requirements | Agent pauses later deltas, builds/sends a consistent snapshot, and retries after reconnect if the notice is lost. |
| `ingest.snapshot` | WebSocket | Agent | Brain | `snapshot_id`, source stream, `through_seq`, complete account-scoped chats/messages | Brain validates and atomically replaces only the fenced stream/account. No ack means safe resend. Invalid non-retryable content gets `ingest.rejected`; transient failure leaves checkpoint unchanged. |
| `ingest.delta` | WebSocket | Agent | Brain | Stable `event_id`, source stream/sequence, one typed raw change | Persisted in Agent outbox until acknowledged. Brain deduplicates and accepts only the next contiguous sequence; gap leads to rejection or `sync.required`. |
| `ingest.ack` | WebSocket | Brain | Agent | Accepted snapshot identity and/or highest contiguous committed source sequence | Agent retains and resends until it observes the ack. Duplicate acks are harmless. |
| `ingest.rejected` | WebSocket | Brain | Agent | Correlation/event identity, validation code, retryable flag, safe detail | Retryable items remain queued with backoff. Non-retryable items block contiguous progress until explicit repair/quarantine policy or resync; no silent skip. |
| `state.snapshot` | WebSocket | Brain | Bridge | Complete canonical conversation/analytics read model and `view_revision` | Sent after every v1 Bridge bind/resync. Bridge stays loading/degraded until valid; reconnect/resync on loss or invalid payload. |
| `state.delta` | WebSocket | Brain | Bridge | Next `view_revision` and an atomic typed change set for conversation/analytics state | Bridge ignores duplicates, applies only the next revision, and sends `state.resync` on a gap or invalid change. |
| `state.resync` | WebSocket | Bridge | Brain | Last applied view revision and reason for recovery | Idempotent. Brain returns `state.snapshot`; Bridge does not claim realtime state while waiting. |
| `presence.observed` | WebSocket | Agent | Brain | Complete normalized online `platform_user_id` list, observation id/time | Ephemeral and never outbox-replayed. Invalid/out-of-order data is ignored/rejected; silence expires to unknown rather than offline. |
| `presence.state` | WebSocket | Brain | Bridge | Authoritative list, `current/unknown` freshness, server receipt/expiry and last-observation metadata | Bridge replaces the presence slice and marks it unknown at `expires_at`. A reconnect receives current state; stale data is never rendered as current. |
| `agent.state` | WebSocket | Brain | Bridge | `connected/stale/disconnected`, active installation metadata, required/applied config revisions, degraded reason | Brain derives it from shared leases/state and sends an initial value. Bridge never substitutes local extension detection; expiry yields stale/disconnected. |
| `system.state` | WebSocket | Brain | Bridge | Account processing mode, readiness/degraded state, safe operational detail | Last value is replaceable state. Bridge marks degraded on expiry/disconnect and receives a fresh value after binding. |
| `protocol.error` | WebSocket | Brain | Agent or Bridge | Error code, correlation/message id, retryability/fatal flag, safe detail | Fatal errors close after delivery attempt. Nonfatal errors leave the relevant checkpoint/revision unchanged; clients follow the indicated retry/resync action. |
| `agent.config.get` | Authenticated HTTP request (`GET /api/v1/agent/config`) | Agent | Brain | Authenticated context, current ETag/revision and supported config schema | Timeout/5xx keeps last known good config and retries. Unauthorized fails the Agent session; `304` reuses validated cached content. |
| `agent.config.document` | Authenticated HTTP response | Brain | Agent | Immutable config revision, schema version, digest/ETag, capture and command policy | Agent rejects invalid/unsupported/digest-mismatched content, keeps last known good config, and reports degraded; it never partially applies. |
| `config.available` | WebSocket | Brain | Agent | Newly required config revision/digest | Signal is idempotent. Loss self-heals because every new `agent.session` repeats the required revision. |
| `config.applied` | WebSocket | Agent | Brain | Applied revision/digest, activation outcome, relevant capability status | Agent repeats applied revision in hello/heartbeat. Brain retains required/applied mismatch and exposes degraded `agent.state` until confirmed. |
| `command.execute` | WebSocket | Brain | Agent | `command_id`, bound account, allowed typed action, deadline, idempotency policy | Agent validates account, allow-list, deadline, and fencing before execution. Duplicate `command_id` returns stored result; Brain does not blindly retry a non-idempotent action without deduplication. |
| `command.result` | WebSocket | Agent | Brain | `command_id`, accepted/succeeded/failed status, safe result/error metadata | Agent persists terminal results until acknowledged. Brain deduplicates; timeout becomes an auditable unknown/failed command state, not a Bridge proxy fallback. |
| `command.result.ack` | WebSocket | Brain | Agent | `command_id` and recorded terminal result identity | Agent may compact the persisted result only after ack; duplicate acks are harmless. |

## Consequences

### Positive

- Sender, receiver, payload purpose, and recovery behavior are reviewable in one place.
- Role-specific schema generation makes invalid directions unrepresentable in normal handlers.
- Each reliability class has one recovery mechanism instead of generic forwarding.
- The matrix directly implements ADRs 0001 through 0005.

### Negative

- Supporting aliases outside the canonical unions would preserve ambiguity and is not allowed.
- Brain needs explicit adapters from ingest commits to the revisioned Bridge read model.
- More message schemas and contract tests are required.
- Product features that assumed direct Bridge-Agent reads need a Brain-owned read API. Bridge command origination is not a protocol v1 extension point.

## Confirmation

- Generate and test four WebSocket discriminated unions and two HTTP config schemas.
- For every matrix row, contract tests must cover the happy path, wrong role, wrong account/fence, duplicate, and stated failure behavior where applicable.
- Exhaustive client routers must fail builds when a union gains an unhandled type.
- An end-to-end test must cover Agent worker termination, Brain restart, Bridge reconnect, presence expiry, config drift, command deduplication, and eventual convergence.
