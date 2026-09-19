# ADR 0032: Retain canonical graph records during SQLite construction

- Status: accepted

## Decision

The built-in SQLite pipeline retains graph records as canonical JSON strings rather than keeping every node and edge as a Python model. The projector emits batches of at most 128 messages. Each batch's models can be released after encoding. Shared identities must have identical contents.

The compact graph belongs to one build and one account. It is not shared between requests, stored as authority, or accepted by the public artifact-staging method. Its digest uses the existing canonical graph JSON format. The ordinary persisted graph tables and publication protocol are unchanged.

The private writer converts records into bounded SQL parameter batches. Existing SQL property checks, endpoint foreign keys, ownership fencing, durable commits, and cancellation remain enabled. Staging and the activation gate independently validate and hash persisted rows. An expected digest is not substituted for checking stored content.

Small conversations can retain full conversation fragments. Compact builds materialize a new fragment only for at most 256 messages and at most half the per-fragment byte limit in encoded graph data. Larger conversations use message-level analyzer reuse. Existing fragment size, count, source-time, and total-byte limits still apply.

Public full-artifact operations and unsupported projectors retain their existing representations. The built-in full projector assembles the same batches for compatibility and clean-build comparisons. Compact mode is an implementation choice, not a new semantic pipeline identity.

## Consequences

This reduces retained Python object overhead. The encoded graph, enrichment arrays, source catalog, and projection document still grow with the account. It is not a constant-memory builder. [ADR 0034](0034-shared-graph-content.md) defines reuse of physical graph content.

No runtime dependency, model, database, migration, or download is added. A complete generation is still written and verified. Source retention, deletion, analysis authorization, and atomically published graph/metric consistency remain required.

See [Compact graph construction](../analytics/compact-graph.md) for implementation and qualification. ADRs 0020, 0028, 0030, and 0031 retain their integrity and publication rules.
