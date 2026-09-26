<!-- CODE-VERIFY: Check sqlite_graph_store.py, projection_activation.py, test_selected_graph_reader.py, test_activation_read_scope.py, test_changed_graph_read_order.py, test_shared_graph_queries.py, test_removed_graph_endpoints.py, graph_membership_pages.py, sql/0020_shared_graph_membership_pages.sql, test_shared_membership_pages.py, shared_graph.py, incremental_graph.py, conversation_graph_units.py, graph_verification.py, validation_receipt.py, database.py, compact_graph.py, sqlite_projection_store.py, sql/0010_shared_graph_segments.sql, sql/0012_generation_content_epoch.sql, sql/0015_shared_graph_delete_guards.sql, sql/0016_incremental_graph_units.sql, sql/0019_graph_reclamation_guards.sql, test_graph_reclamation.py, test_shared_graph.py and test_incremental_graph_units.py before changing storage, verification or limit claims. -->

# Reuse stored graph content

The built-in SQLite path selects immutable stored content for a complete generation. Unchanged content is not inserted again. Source data, analyzer results and public graph identities keep their existing meanings.

## Records and segments

Node content is keyed by its account and canonical record digest. Edge content includes references to the stable identities of both endpoint nodes. Foreign keys require those endpoints to exist. Property-validation triggers remain enabled.

Each node or edge belongs to one of 256 identity-prefix buckets. A segment contains one kind and bucket. A generation selects at most 512 segments. The ordinary writer calculates each segment digest from the current logical graph, then reuses a matching sealed segment from a completed active predecessor. Other segments are written under the ordinary lease and durable batch transactions. Under [ADR 0039](../adr/0039-incremental-conversation-graph-units.md), an eligible update can instead keep unchanged verified segments and rebuild only affected buckets.

A covering index lets insert guards locate open segments by account, record kind, bucket, and build state. The guards remain enabled.

Membership rows select the exact records in a segment. SQL prevents mutation of sealed membership and content. It also prevents mixing generation-owned rows with shared segments in one generation. Schema version 10 exposes both storage layouts through the `graph_nodes` and `graph_edges` views. Schema version 15 also blocks deletion of referenced graph content and segments when foreign-key enforcement is disabled.

[ADR 0042](../adr/0042-shared-graph-membership-pages.md) lets segments share immutable membership pages. Exact predecessor membership is compared before reuse; changed pages are written separately. The graph digest still binds the same ordered records.

The candidate still undergoes persisted-content verification before validation and final activation. Cold builds and missing proofs read and validate every selected graph row. [ADR 0038](../adr/0038-verified-graph-segment-reuse.md) permits a same-process incremental build to reuse a proof for unchanged immutable segments from the exact active predecessor. New or changed segments are validated from their actual stored rows. A supplied segment name, digest or caller-provided proof is never sufficient.

## Scoped verification reads

Bounded degree and root-traversal reads start from indexed incident content, then check exact membership in the selected generation. Both endpoints retain the existing time and kind filters. Direction, cancellation, result limits and generation-change checks remain.

Ordered reads start from the selected generation's manifest, then resolve its segment membership and content. They use segment, membership-page and record order directly instead of sorting a scan of every segment retained for the account.

Endpoint verification compares selected edge endpoints with the candidate's selected node identities. Cold builds, restart and proof fallback run complete endpoint closure for the selected generation. A same-process ADR 0039 update with an exact ADR 0038 predecessor proof can reuse closure for unchanged edge segments. It still checks every changed edge segment against the candidate node manifest. Removed-node checks start at the account-scoped source and target indexes, then require the exact edge identity and content version in the candidate generation. Missing proof or an unknown removal set uses the complete scan.

Small changed-segment checks may read content in content-key order before verifying it in canonical graph order. Every selected row still undergoes property, identity and content-hash validation. Endpoint checks may use those same transaction-local rows, but still resolve every endpoint against the selected generation. A write on that connection invalidates the read buffer. Read-ahead stops at 32,768 rows or 32 MiB of row and membership values; larger sets use the streaming verifier. These bounds exclude Python container overhead.

When staging conversation-page references, witness checks share one canonical database connection in autocommit mode. Each check issues a new query and closes its cursor. No witness result or read transaction is retained between checks. The connection closes on success, failure or cancellation. Other threads and repositories use their own connections.

## Physical write order

Content is inserted in content-hash order, matching its primary key, rather than in unrelated graph-identity order. Sorted per-bucket iterators are merged without retaining another complete set of record payloads. Public graph ordering, identities and digest bytes do not change.

Within each existing write transaction, lookups of at most 256 keys skip content already present. New content still passes the SQL property and ownership guards. New membership pages are written for changed parts of each segment, and the selected graph still undergoes its required persisted-content verification. Identity-write statistics count newly absent identities rather than unrelated trigger updates.

The content writer temporarily requests a page-cache target of 512 bytes per logical record. Full graph construction adds 1 KiB for each selected membership page. The combined target stays between 16 MiB and 128 MiB. Its prior connection setting is restored after success, failure or cancellation. This is a bounded I/O working-set tradeoff, not a total memory cap or laptop-capacity claim. Input graph objects and sort-key lists still consume additional memory.

## Cleanup and recovery

Deleting a retired generation removes its segment references. Segments still referenced by another generation remain. Removing the final reference reclaims the segment and content that no remaining segment or edge needs. Referenced content and segments cannot be deleted directly, even with foreign-key enforcement disabled. Foreign keys preserve endpoint ordering, and an interrupted transaction rolls back cleanup.

Schema version 19 checks for remaining account-scoped references before opening a content record for deletion. Shared content is left untouched. The final reference still triggers content and endpoint reclamation in the same transaction.

Existing source-time expiry and deletion behavior continue to govern generation visibility and reclamation. Partial builds cannot become readable. Restart requires the same completed canonical witness and verifies stored content through the logical views.

## Incremental construction and remaining work

[ADR 0039](../adr/0039-incremental-conversation-graph-units.md) lets an eligible same-process update avoid restoring and merging unchanged graph records. Conversation-local membership units identify unchanged graph content, verified segment chunks provide canonical bytes for unchanged buckets, and only affected buckets are rebuilt. Cold builds, restart without live proofs, unsupported cache data and fallback paths still assemble the complete logical graph.

Incremental construction does not eliminate account-wide projection metadata, analyzer-reference staging, source identity checks, endpoint verification or publication work. Its cache bounds are not a total process-memory guarantee.

The migrations add tables, indexes and triggers inside the encrypted analytics database. They add no runtime dependency, network service or database file. Installer size and supported laptop capacity require separate measurements.
