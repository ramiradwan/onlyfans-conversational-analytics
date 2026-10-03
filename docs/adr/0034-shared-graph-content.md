# ADR 0034: Share immutable graph content across generations

- Status: accepted

## Decision

Keep a complete logical graph for each analytics generation, but let generations reference unchanged physical records. Store content and generation membership in the existing encrypted analytics database. The canonical digest and graph query contracts do not change.

Node and edge content is immutable. An edge refers to the stable identities of both endpoints through foreign keys. A segment groups records by kind and the first two hexadecimal digits of their stable identity. A generation selects at most one segment per kind and bucket.

The built-in SQLite writer computes segment identities from current graph content. It can reference matching sealed segments from a completed active predecessor. It creates other segments under the existing writer lease. Referencing stored content is not a substitute for checking it: staging and activation independently verify the complete selected graph, including properties, endpoints, counts and digests.

SQL views expose the complete generation with its original columns. Ordinary graph-writer calls retain generation-owned records and their SQL guards. A generation cannot mix those records with segment membership. Persisted graphs present before the migration remain readable through the same views.

## Lifetime and visibility

Membership can be added only to a building generation. Sealed segments and their membership are immutable. Removing a retired generation removes its membership. A segment disappears only after its last generation reference; its unreferenced content and endpoints are then reclaimed through foreign-key-safe cleanup.

Publication still validates the canonical witness and atomically switches the active generation. Reads cannot combine rows from different generations. Existing source expiry and deletion checks apply to the entire selected graph. No second database, writer process, model or runtime dependency is added.

## Consequences

Changed graph content and segment membership replace full physical graph rewrites on the built-in path. The builder still constructs the complete logical graph, and candidate verification still reads it. Full projection documents and aggregate processing also remain account-wide costs. This decision does not establish constant memory or the workload acceptance targets.

This changes physical storage only within ADR 0020's generation model. ADRs 0019, 0028, 0030 and 0031 retain their encryption, source, verification and publication requirements. See [Shared graph storage](../analytics/shared-graph-storage.md) for execution and qualification details.
