<!-- CODE-VERIFY: Verify protocol roles, WebSocket endpoints, direct Bridge-to-Agent binding, HTTP routes, versioning, durability, resynchronization, and ephemeral-state behavior against source, shared fixtures, and tests before editing. -->

# Communication overview

This page summarizes how Agent, Brain, and Bridge communicate. Accepted architecture decisions are authoritative when this overview differs from them.

The canonical operation matrix is [ADR 0006](docs/adr/0006-canonical-communication-matrix.md).

## Roles

- **Agent → Brain:** captured conversation changes, presence observations, synchronization data, applied configuration state, and command results.
- **Brain → Agent:** session state, acknowledgements, synchronization requests, configuration updates, and allowed commands.
- **Brain → Bridge:** conversation summaries, analytics, presence, Agent status, and system readiness.
- **Bridge → Brain:** connection setup, state-resynchronization requests, and authenticated HTTP requests for paged history, settings, and Agent pairing.
- **Bridge → Agent:** one browser-extension binding message carrying the account-bound Agent pairing ticket and creator-account identifier.

The direct Bridge-to-Agent binding path does not carry raw ingestion or Agent commands.

## Connection rules

Agent and Bridge use separate WebSocket endpoints: `/ws/agent` and `/ws/bridge`.

Each WebSocket connection starts with a role-specific hello message. Brain authenticates the connection and binds it to one creator account for the lifetime of that connection.

The current WebSocket protocol version is `2`. The Agent configuration schema also uses version `2`, but those versions are independent of extension, database, or signer versions.

Agent ingestion is durable: pending changes remain local until Brain acknowledges them. Brain deduplicates retries and requires ordered progress.

Bridge receives bounded snapshots and revisioned updates. If it detects a revision gap, it requests a fresh snapshot instead of guessing what was missed.

Presence and heartbeat data are temporary state. They expire rather than being replayed as durable history.

## HTTP routes

Brain also exposes authenticated HTTP routes for data that should not travel in WebSocket state, including:

```text
POST /api/v1/agent/pairing
GET /api/v1/agent/config
GET /api/v1/conversations/{conversation_id}/messages
GET /api/v1/settings/history
PUT /api/v1/settings/history
DELETE /api/v1/settings/history/consent
```

Historical messages are paged. Complete message history is not copied into Bridge WebSocket snapshots.

Provisioning and WebAuthn use separate same-origin HTTP surfaces and are not protocol-v2 WebSocket operations.

## Authoritative decisions

- [ADR 0003](docs/adr/0003-immutable-socket-identity.md) — connection role and account identity.
- [ADR 0004](docs/adr/0004-durable-reconnect-resync.md) — durable delivery and resynchronization.
- [ADR 0005](docs/adr/0005-agent-configuration-versioning.md) — Agent configuration updates.
- [ADR 0006](docs/adr/0006-canonical-communication-matrix.md) — canonical sender, receiver, transport, and failure behavior for each operation.
- [ADR 0008](docs/adr/0008-production-authentication.md) — provisioning and runtime authentication.
- [ADR 0010](docs/adr/0010-signer-history-acquisition-and-bounded-state.md) — history acquisition, bounded repair, and paged Bridge history.
