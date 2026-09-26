<!-- CODE-VERIFY: Check conversation_reuse.py, conversation_graph_units.py, conversation_graph_unit_sql.py, incremental_graph.py, shared_graph.py, compact_graph.py, sqlite_projection_store.py, sql/0016_incremental_graph_units.sql, sql/0018_graph_chunk_metadata.sql, test_graph_chunk_metadata.py, test_incremental_graph_units.py, test_shared_graph.py and test_conversation_graph_receipts.py before changing reuse or fallback claims. -->

# ADR 0039: Reuse immutable conversation graph units

- Status: accepted

## Decision

The built-in compact SQLite path may assemble an incremental account graph from immutable conversation graph units and verified shared-segment chunks instead of reconstructing every unchanged graph record in Python.

Schema version 16 stores one optional conversation graph unit per retained conversation. A generation reference binds that unit to the conversation input/configuration identity, retention window, participant reference, start/end times, graph digest and record counts. The unit stores only sorted opaque node and edge identities. It does not store source text or native identifiers.

A cold or fallback build still projects the complete logical graph. While shared graph segments are written, the store may also retain bounded canonical segment chunks. A process-local ADR 0038 proof binds those chunks to graph rows that were already verified from persisted content.

Schema version 18 adds a metadata index for chunk-availability checks. The query selects the predecessor generation and compares each chunk's identity, kind, count and digest with its proof. Opening a chunk still checks its payload hash.

On a later build, unchanged conversations may contribute their witnessed unit references without reopening predecessor graph rows. Changed conversations are projected normally. The builder compares changed canonical record hashes with the active predecessor, rebuilds only affected identity buckets, updates conversation-order edges from current metrics and witnessed predecessor timeline metadata, then combines rebuilt buckets with unchanged verified segment chunks.

The incremental result must reproduce the ordinary canonical graph digest, per-kind counts and endpoint closure. When the exact predecessor segment proof is available, unchanged edge segments can retain their previously verified closure. Changed edge buckets are checked against the current selected node manifest, and every removed predecessor node is checked for surviving selected edge references. Otherwise validation runs the complete endpoint scan. Publication still uses the existing generation witness, ownership fencing, staging validation and activation receipt rules.

## Eligibility and fallback

Incremental assembly is available only when the exact completed active predecessor has both a process-local conversation-unit proof and an ADR 0038 graph-segment proof, and every referenced segment has a verified canonical chunk. The reviewed schema/store identity must still match.

Missing, malformed, expired or over-budget conversation units, unavailable segment chunks, restart, unsupported schema, changed configuration, failed proof checks or any cache inconsistency falls back to the existing full graph construction or rejects the optional cache without weakening publication. Public projection currentness is not relaxed; stale predecessor data is used only inside the private rebuild path.

Conversation graph units and segment chunks are immutable while referenced. Direct deletion is blocked even when foreign-key enforcement is disabled. Retirement removes generation references and reclaims optional cache content after its final reference disappears.

## Bounds

At most 4,096 conversation graph units are retained per build. Newly stored unit payloads are bounded to 128 MiB in aggregate. Each compressed node-ID or edge-ID payload is bounded to 64 MiB and four million identities. A canonical segment chunk is bounded to 64 MiB.

These are cache/admission bounds, not a total process-memory guarantee. Exceeding them disables incremental reuse for the affected data and keeps ordinary analysis available.

## Consequences

A small source change can avoid account-wide graph record restoration and merging. The updater still computes current conversation metrics, validates reused page/analyzer data, stages generation references, verifies changed graph buckets, proves endpoint closure for changed edges and removed nodes, and publishes a complete generation. Fallback validation checks the complete selected endpoint relation.

Cold builds, restart without process-local proofs, explicit artifact reads and backup verification retain their complete graph paths. The optimization adds one rebuildable analytics migration and no dependency, database file, network service or writer process.
