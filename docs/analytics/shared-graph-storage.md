<!-- CODE-VERIFY: Check shared_graph.py, graph_verification.py, validation_receipt.py, database.py, compact_graph.py, sqlite_projection_store.py, sql/0010_shared_graph_segments.sql, sql/0012_generation_content_epoch.sql, sql/0015_shared_graph_delete_guards.sql, and test_shared_graph.py before changing storage, verification or limit claims. -->

# Reuse stored graph content

The built-in SQLite path selects immutable stored content for a complete generation. Unchanged content is not inserted again. Source data, analyzer results and public graph identities keep their existing meanings.

## Records and segments

Node content is keyed by its account and canonical record digest. Edge content includes references to the stable identities of both endpoint nodes. Foreign keys require those endpoints to exist. Property-validation triggers remain enabled.

Each node or edge belongs to one of 256 identity-prefix buckets. A segment contains one kind and bucket. A generation selects at most 512 segments. The writer calculates each segment digest from the current logical graph, then reuses a matching sealed segment from a completed active predecessor. Other segments are written under the ordinary lease and durable batch transactions.

A covering index lets insert guards locate open segments by account, record kind, bucket, and build state. The guards remain enabled.

Membership rows select the exact records in a segment. SQL prevents mutation of sealed membership and content. It also prevents mixing generation-owned rows with shared segments in one generation. Schema version 10 exposes both storage layouts through the `graph_nodes` and `graph_edges` views. Schema version 15 also blocks deletion of referenced graph content and segments when foreign-key enforcement is disabled.

The candidate still undergoes persisted-content verification before validation and final activation. Cold builds and missing proofs read and validate every selected graph row. [ADR 0038](../adr/0038-verified-graph-segment-reuse.md) permits a same-process incremental build to reuse a proof for unchanged immutable segments from the exact active predecessor. New or changed segments are validated from their actual stored rows. A supplied segment name, digest or caller-provided proof is never sufficient.

## Scoped verification reads

Ordered reads start from the selected generation's manifest, then resolve its segment membership and content. They use bucket and record order directly instead of sorting a scan of every segment retained for the account.

Endpoint verification compares the selected edges' endpoint identities with the selected nodes' actual stored identities. It does not accept an endpoint merely because another generation retains it. SQLite keeps a temporary set of identities for this comparison. Full verification streams graph properties and captures a bounded process-local segment proof in the same scan. A valid ADR 0038 proof can reuse only unchanged segment results; complete endpoint closure still runs for the selected generation.

## Physical write order

Content is inserted in content-hash order, matching its primary key, rather than in unrelated graph-identity order. Sorted per-bucket iterators are merged without retaining another complete set of record payloads. Public graph ordering, identities and digest bytes do not change.

Within each existing write transaction, lookups of at most 256 keys skip content already present. New content still passes the SQL property and ownership guards. Membership is written for each changed segment, and the selected graph still undergoes its required persisted-content verification. Identity-write statistics count newly absent identities rather than unrelated trigger updates.

The content writer temporarily requests a page-cache target of 512 bytes per logical record, bounded between 16 MiB and 128 MiB. Its prior connection setting is restored after success, failure or cancellation. This is a bounded I/O working-set tradeoff, not a total memory cap or laptop-capacity claim. Input graph objects and sort-key lists still consume additional memory.

## Cleanup and recovery

Deleting a retired generation removes its segment references. Segments still referenced by another generation remain. Removing the final reference reclaims the segment and content that no remaining segment or edge needs. Referenced content and segments cannot be deleted directly, even with foreign-key enforcement disabled. Foreign keys preserve endpoint ordering, and an interrupted transaction rolls back cleanup.

Existing source-time expiry and deletion behavior continue to govern generation visibility and reclamation. Partial builds cannot become readable. Restart requires the same completed canonical witness and verifies stored content through the logical views.

## Remaining account-wide work

The builder still assembles the complete logical graph. It hashes record content to choose segments, writes the projection document, and verifies the selected graph. Physical reuse does not make the full operation proportional only to the changed messages or bound total process memory.

The migration adds tables, indexes, views and triggers inside the encrypted analytics database. It adds no model, runtime dependency, network service or database file. Installer size and supported laptop capacity require separate measurements.
