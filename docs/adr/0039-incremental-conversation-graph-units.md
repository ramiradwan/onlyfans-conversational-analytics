<!-- CODE-VERIFY: Check conversation_integrity.py, conversation_integrity_store.py, test_incremental_conversation_integrity.py, sql/0021_conversation_integrity.sql, conversation_graph_stream.py, test_predecessor_graph_handoff.py, recovered_reuse.py, conversation_append.py, test_recovered_update_reuse.py, test_dominant_append_reuse.py, conversation_reuse.py, conversation_graph_units.py, conversation_graph_unit_sql.py, incremental_graph.py, shared_graph.py, compact_graph.py, sqlite_projection_store.py, sql/0016_incremental_graph_units.sql, sql/0018_graph_chunk_metadata.sql, test_graph_chunk_metadata.py, test_incremental_graph_units.py, test_shared_graph.py and test_conversation_graph_receipts.py before changing reuse or fallback claims. -->

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

Missing, malformed, expired or over-budget conversation units, unavailable segment chunks, restart without freshly established proofs, unsupported schema, changed configuration, failed proof checks or any cache inconsistency falls back to the existing full graph construction or rejects the optional cache without weakening publication. Public projection currentness is not relaxed; stale predecessor data is used only inside the private rebuild path.

Conversation graph units and segment chunks are immutable while referenced. Direct deletion is blocked even when foreign-key enforcement is disabled. Retirement removes generation references and reclaims optional cache content after its final reference disappears.

## Verified preparation and append reuse

Restart still discards process-local proofs. Normal scheduler preparation may establish new proofs after complete persisted generation validation. It independently reconstructs each conversation's graph from current canonical records and the validated stored enrichment. Exact unit membership and every selected stored graph record must match. The store stamp, completed witness, source identity and retention are rechecked before retaining metadata. No classifier runs and no new generation is published during this read-only preparation.

For a large dominant conversation, one message appended in canonical order may reuse a verified unchanged prefix. The complete canonical prefix digest, account, configuration, source times and predecessor proofs must match. Only message-local analyzers qualify. Equal timestamps are permitted only when the complete canonical prefix still matches; query uncertainty for tied events is unchanged. The builder reads verified prefix graph bytes, constructs the boundary and new-message graph, recalculates conversation metrics and stages the ordinary complete generation. An edit, deletion, late arrival, context-dependent analyzer, custom projector or missing proof uses the existing full path. This adds no graph representation or storage format.

Periodic currentness checks may use these same complete-content proofs after rechecking every generation binding, the full enrichment stamp, source identity, completed witness and retention. This avoids rematerializing immutable records after the positive-currentness cache expires. Its 60-second lifetime is unchanged; missing proofs require full verification.

The append reader may frame already verified canonical graph bytes without rebuilding property dictionaries. The actual chunk hash must match the live complete-content proof. Record scope, identities, counts, categories and the selected conversation digest are rechecked. A missing proof or mismatch prevents this reuse; the stored format is unchanged.

A version-1 append streams the selected predecessor members instead of retaining a complete conversation graph. The same pass verifies the old canonical digest and computes the new canonical digest. Unit membership, digest encoding and stored format are unchanged. Only the boundary records enter changed-graph assembly; a refused optional unit keeps the complete graph/page fallback.

## Internal checksum version 2

Schema 21 adds an explicit checksum version and bounded integrity metadata to optional conversation graph units. Version 1 keeps its canonical-payload checksum. Version 2 binds the account, conversation, ordered node/edge identities and exact content hashes through stable two-hex-digit groups. Each group holds at most 16,384 identities. The canonical metadata is limited to 256 KiB and counts against the existing aggregate unit budget. A refused optional group uses the complete fallback.

Assembly reads only the witnessed headers of unchanged units. Membership payloads remain available for independent stored verification, deletion handling and full fallback.

Only the optional unit checksum changes. Public graph identities, graph records, query semantics, pipeline graph digests and source retention do not change. Legacy units remain readable. On schema 21, normal startup preparation requests an ordinary authorized build when active legacy units remain. That build replaces them with version 2, or omits optional units that exceed their bounds. It never relabels a legacy checksum or changes a published generation in place. The next readiness check does not request another upgrade. Restart preparation reconstructs the version declared by each unit. An older binary rejects the newer migration ledger rather than misreading version-2 metadata. Migration backups retain the previous schema.

An eligible append reads exact predecessor content-version metadata for touched groups, compares their summaries with the verified predecessor, and derives replacement groups. It does not reopen unchanged graph payload buckets. The existing canonical-prefix, account, configuration, source-time and proof checks still apply. Changing a previously selected record is allowed only for the conversation boundary record; other prefix changes fail closed.

Generation validation checks version-2 summaries against the selected persisted content after ordinary graph-byte and endpoint verification. A group can retain prior verification only when both its summary and its independently verified generation segment remain unchanged under the exact predecessor binding. Other groups are checked against exact selected content versions. The temporary metadata cache stays within the existing 32,768-row and 32 MiB verification bounds and lives only within that verification operation. A stored digest alone never establishes readiness or authority.

Restart, invalidated proof, changed schema or changed selection requires independent verification. Activation and cleanup keep their existing fenced, synchronous paths. No proof lifetime, request limit or retention period is extended.

## Bounds

At most 4,096 conversation graph units are retained per build. Newly stored unit payloads are bounded to 128 MiB in aggregate. Each compressed node-ID or edge-ID payload is bounded to 64 MiB and four million identities. A canonical segment chunk is bounded to 64 MiB.

These are cache/admission bounds, not a total process-memory guarantee. Exceeding them disables incremental reuse for the affected data and keeps ordinary analysis available.

## Consequences

A small source change can avoid account-wide graph record restoration and merging. The updater still computes current conversation metrics, validates reused page/analyzer data, stages generation references, verifies changed graph buckets, proves endpoint closure for changed edges and removed nodes, and publishes a complete generation. Fallback validation checks the complete selected endpoint relation.

Cold builds, restart without process-local proofs, explicit artifact reads and backup verification retain their complete graph paths. The optimization adds one rebuildable analytics migration and no dependency, database file, network service or writer process.

When shared graph storage is disabled, construction retains version-1 optional units and does not request an integrity upgrade. Page-cache fallback still validates its canonical-payload checksum through the complete page reader; a version-2 integrity root is never compared as though it were that checksum.
