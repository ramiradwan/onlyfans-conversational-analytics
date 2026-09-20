<!-- CODE-VERIFY: Check conversation_pages.py, conversation_page_sql.py, conversation_reuse.py, conversation_sql.py, sqlite_projection_store.py, sql/0011_conversation_pages.sql, sql/0013_shared_conversation_pages.sql, validation_receipt.py, test_conversation_pages.py and test_shared_conversation_pages.py, conversation_graph_sql.py, test_conversation_graph_references.py and test_conversation_graph_receipts.py and test_encoded_conversation_graph.py before editing behavior or limits. -->

# Reuse large conversation results

The compact SQLite path stores large conversation results in pages rather than requiring a complete model-based fragment. This avoids loading and re-projecting an unchanged large conversation during the next admitted build. It does not change public question or graph meanings.

## Bounds and storage

A page holds at most 256 records and 256 KiB. A conversation may use at most 4,096 pages. The header and pages count toward the existing 64 MiB per-build fragment limit and 4,096-conversation limit; ordinary fragments share those limits. Oversized records or exhausted capacity prevent retention, not analysis.

`conversation_page_sets` holds each generation's header. Schema 13 stores immutable compressed bytes in `conversation_page_content`, keyed by account and content hash. `conversation_page_refs` selects the ordered pages for each generation. The `conversation_pages` view also reads generation-owned pages from older databases. The header covers input and configuration identity, metrics, expiry, counts and the ordered-page checksum. Pages contain findings, graph records or references, and analyzer reuse records. They do not copy source text or native message identifiers.

Only a completed, active generation supplies pages. Missing, malformed, misordered, incompatible or expired pages cause a source rebuild. Parsing is limited to one page at a time, but the restored conversation output and assembled account graph are still retained. These limits are not a total memory cap.

## Graph references

With shared graph storage, `zlib-json-graph-ids.v2` pages store node and edge IDs. Their header records the exact conversation graph digest. `zlib-json.v1` pages remain self-contained and readable. Switching the storage mode converts valid cached output without repeating source reads or analysis.

Each lookup selects at most 256 IDs through the account and generation bucket, then validates the actual graph columns and content hashes. Restoration retains those checked canonical bytes without reconstructing node or edge models. It checks account scope, duplicate identities, conversation-local endpoints and the complete graph digest. Staging compares the records with the candidate; checksums alone cannot establish a match.

Reads and page staging use a temporary 32 MiB SQLite page-cache target and restore its previous setting on exit. This is separate from the unchanged 64 MiB application cache budget and is not a total process-memory limit.

A successful restore may carry a process-local graph-read receipt. It binds the exact generation and header to the storage stamp observed before and after the graph read. Staging compares that stamp inside its write transaction, before making its own changes. An exact match avoids a second source-graph read; otherwise staging rereads the rows. The page bytes, header and completed witness are always reopened and checked. The complete persisted candidate is still independently verified.

## Correctness and lifetime

An unchanged page set leaves only its header and predecessor generation reference in build state. Staging reopens that reference inside its write transaction. It requires the same active generation and completed canonical witness, checks the header, and compares every page record with the candidate artifact. Analyzer results and graph endpoints remain checked. An unavailable or changed reference aborts staging without publishing a partial generation.

Existing shared bytes are compared before reuse. Staging writes only references for matching content. New content is inserted once. If an existing content key holds damaged bytes, recomputed output uses generation-owned pages instead of overwriting that predecessor. Retirement can then remove the damaged cache safely.

Rows are immutable after insertion. Retirement or generation deletion removes that generation's page references. Shared bytes remain until the last reference disappears. Cancellation rolls back staged writes or discards the candidate without removing the active predecessor's pages. Partial or discarded builds cannot seed reuse.

Shared page content and references participate in the existing storage-change counter. Missing tracking or an unreviewed schema disables validation receipts. Full stored-graph verification, source-time expiry, and atomic publication remain required.

An edit, late arrival, deletion, changed participant, model/configuration change, or expired source window prevents whole-conversation reuse. Analyzer records preserved in a successful page hit remain available to the separate message-level cache on later edits. Authorization and independent stored-content verification remain required.

## Qualification

Run `python -m pytest tests/test_conversation_pages.py tests/test_shared_conversation_pages.py tests/test_conversation_graph_references.py tests/test_conversation_graph_receipts.py tests/test_encoded_conversation_graph.py` and the [analytics baseline](qualification.md). Tests include independent full-build comparisons, page boundaries, source mutations, configuration, missing/corrupted pages, staging tamper, budgets, cancellation, expiry and restart.

Measure cold construction and changed-message publication separately. Count conversation-body reads, projector calls, analyzer calls, retained bytes and database writes; do not infer a speedup solely from fewer analyzer calls. Compare identical source and workloads with page reuse available and unavailable. Laptop, installer, production classification and 100,000-message update qualification remain separate gates.

Pages use standard-library compression. Both stored bytes and decompressed output are bounded to 256 KiB. Truncated, trailing or oversized streams are rejected. The existing 64 MiB retention budget counts compressed pages and headers; decoding and assembled outputs consume additional memory.

## Remaining work

References still charge their complete compressed payload against the 64 MiB cache budget. This change does not increase coverage beyond that limit. Small fragments and the separate analyzer cache still copy their records. Page restoration still decodes findings, graph IDs and stored properties. It reuses checked graph bytes, but the builder still assembles the complete account graph. Physical page sharing does not establish the 100,000-message update target or a total process-memory bound.
