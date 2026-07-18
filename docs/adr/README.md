# Architecture decision records

These decision records define the Agent-Brain-Bridge communication architecture, authentication boundary, and local runtime topology.

## Index

- [ADR 0001: Make Agent the sole raw-ingestion producer](0001-agent-owned-raw-ingestion.md) — Agent alone emits raw snapshots/deltas; Brain persists and derives; Bridge only consumes Brain state.
- [ADR 0002: Make Brain the authority for derived presence](0002-brain-derived-presence.md) — Agent reports expiring observations while Brain derives platform presence and Agent liveness for Bridge.
- [ADR 0003: Bind immutable role and account identity to every socket](0003-immutable-socket-identity.md) — Role-specific handshakes bind an authorized creator account and require reconnect/resync on identity change.
- [ADR 0004: Use durable cursors, idempotent deltas, and explicit resynchronization](0004-durable-reconnect-resync.md) — Durable Agent outbox and Brain checkpoints make MV3 termination/reconnect safe; Bridge recovers revision gaps by snapshot.
- [ADR 0005: Make Brain the versioned Agent-configuration authority](0005-agent-configuration-versioning.md) — Brain serves immutable REST config while WebSocket state reports required/applied drift without coupling config to DB migrations.
- [ADR 0006: Adopt the canonical Agent-Brain-Bridge communication matrix](0006-canonical-communication-matrix.md) — The role-specific matrix assigns one sender, receiver, payload contract, and failure behavior to every operation.
- [ADR 0007: Use a static authentication ticket for local development](0007-stub-auth-for-dev.md) — Superseded by ADR 0008; the non-secret ticket is restricted to explicit local-development mode.
- [ADR 0008: Separate hosted customer provisioning from local runtime authentication](0008-production-authentication.md) — A dedicated external CIAM tenant and signed grants provision installations while runtime authentication and conversation data remain local.
- [ADR 0009: Use a local-first production topology and explicit persistence boundary](0009-local-first-topology-and-persistence.md) — Accepted single-machine topology with a loopback Brain, conditional `http://bridge.localhost:17871` Bridge host, authoritative SQLite stores, rebuildable projections, and in-process event distribution.
