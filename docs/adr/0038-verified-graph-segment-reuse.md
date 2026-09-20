<!-- CODE-VERIFY: Check shared_graph.py, graph_verification.py, compact_graph.py, sqlite_projection_store.py, sqlite_graph_store.py, validation_receipt.py, sql/0010_shared_graph_segments.sql, sql/0012_generation_content_epoch.sql, sql/0015_shared_graph_delete_guards.sql and test_shared_graph.py before changing proof or fallback claims. -->

# ADR 0038: Reuse verified immutable graph segments

- Status: accepted

## Decision

The built-in SQLite staging path may reuse a process-local verification proof for unchanged shared graph segments selected from the completed active predecessor. New or changed segments still validate their persisted rows in full.

A full graph verification creates the proof while it already scans persisted graph rows. For each sealed segment it binds the record kind and bucket, segment identity, segment digest, record count and category counts to the actual canonical row bytes. The proof also binds the predecessor generation metadata and the reviewed storage schema.

Schema version 15 prevents deletion of referenced graph content and referenced segments even when SQLite foreign-key enforcement is disabled. Existing triggers already prevent updates to graph content, sealed segment membership and sealed segment metadata. A schema change changes the SQLite schema cookie and makes an existing proof unusable.

An incremental build still computes its complete logical graph and segment digests. The candidate manifest must exactly match the selected persisted manifest. A reused segment is accepted only when the same segment identity, digest and count appear in a proof for the exact active predecessor. New or changed segments recompute canonical row bytes, content hashes, segment digests and category counts from persisted rows.

Complete endpoint closure, account scope, projection validation, persisted counts, the public graph digest, canonical witness, ownership fencing and activation checks remain required. A missing proof, restart, generation mismatch, schema change, unsupported catalog or candidate with no reusable segment uses the ordinary complete graph-row verification path. Full verification captures a replacement proof without adding another graph scan.

## Lifetime and trust boundary

Proofs exist only in the projection-store process and are bounded to eight generations. They are not persisted and are never accepted from callers.

The proof does not require an unchanged global content epoch because building another generation legitimately changes that epoch. Safety instead depends on the referenced segment being immutable while it remains selected by the active predecessor. The store identity, schema identity and schema cookie must still match.

The database trust boundary is unchanged. A privileged process that rewrites the encrypted file outside SQLite or deliberately replaces reviewed schema controls is outside this proof contract.

## Consequences

Incremental validation can avoid rereading and revalidating unchanged graph payloads. It still verifies changed segment rows and the complete selected endpoint relation.

Cold builds, process restarts, backup verification and explicit artifact reads retain complete row validation. Logical account-graph construction is unchanged and remains account-sized work.

The change adds one rebuildable analytics migration and no dependency, model, database file, network service or writer process.
