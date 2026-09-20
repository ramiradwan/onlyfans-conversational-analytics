<!-- CODE-VERIFY: Check conversation_pages.py, conversation_page_sql.py, conversation_reuse.py, conversation_sql.py, sqlite_projection_store.py, sql/0011_conversation_pages.sql, sql/0013_shared_conversation_pages.sql, validation_receipt.py, test_conversation_pages.py and test_shared_conversation_pages.py, conversation_graph_sql.py, test_conversation_graph_references.py and test_conversation_graph_receipts.py, test_encoded_conversation_graph.py and test_small_conversation_pages.py before editing behavior or limits. -->

# Reuse conversation results

The compact SQLite path stores conversation results in bounded pages, regardless of message count. An unchanged conversation can reuse its pages without loading source messages or projecting its graph again. Public question and graph meanings do not change.

Valid full fragments are converted to pages without repeating source reads or analysis. Their original source cutoff and expiry remain binding. Stores without page support and full-model builds use the fragment path.

## Bounds and storage

A page holds at most 256 records and 256 KiB. A conversation may use at most 4,096 pages. The header and pages count toward the existing 64 MiB per-build fragment limit and 4,096-conversation limit; ordinary fragments share those limits. Oversized records or exhausted capacity prevent retention, not analysis.

`conversation_page_sets` holds each generation's header. Schema 13 stores immutable compressed bytes in `conversation_page_content`, keyed by account and content hash. `conversation_page_refs` selects the ordered pages for each generation. The `conversation_pages` view also reads generation-owned pages from older databases. The header covers input and configuration identity, metrics, expiry, counts and the ordered-page checksum. Pages contain findings, graph records or references, and analyzer reuse records. They do not copy source text or native message identifiers.

Only a completed, active generation supplies pages. Missing, malformed, misordered, incompatible or expired pages cause a source rebuild. Parsing is limited to one page at a time, but the restored conversation output and assembled account graph are still retained. These limits are not a total memory cap.

## Graph references

With shared graph storage, `zlib-json-graph-ids.v2` pages store node and edge IDs. Their header records the exact conversation graph digest. `zlib-json.v1` pages remain self-contained and readable. Switching the storage mode converts valid cached output without repeating source reads or analysis.

Each lookup selects at most 256 IDs through the account and generation bucket, then validates the actual graph columns and content hashes. Restoration retains those checked canonical bytes without reconstructing node or edge models. It checks account scope, duplicate identities, conversation-local endpoints and the complete graph digest. The ordinary staging path compares the records with the candidate. ADR 0037 may reuse that same build's completed comparison only under its exact tracked storage proof; checksums alone cannot establish a match.

Reads and page staging use a temporary 32 MiB SQLite page-cache target and restore its previous setting on exit. This is separate from the unchanged 64 MiB application cache budget and is not a total process-memory limit.

A successful graph-reference restore may carry a process-local same-build receipt. Its storage stamp is captured before the header and compressed pages are read and must still match after the complete restore. The receipt has a process-local MAC over the predecessor generation, full header and storage stamp. Staging compares that proof inside its write transaction, before making its own changes. Under [ADR 0037](../adr/0037-same-build-page-verification.md), an exact proof for the same completed active predecessor can copy existing shared page references without decoding those pages again. Missing, forged or changed proof uses the ordinary reopen and candidate-comparison path. The complete persisted candidate is still independently verified.

## Correctness and lifetime

An unchanged page set leaves only its header and predecessor generation reference in build state. Staging reopens that reference inside its write transaction and requires the same active generation and completed canonical witness. The ordinary path reopens every page and compares its records with the candidate artifact. A valid ADR 0037 same-build receipt instead proves that comparison was already completed under the unchanged tracked storage state and copies only an existing shared-reference layout. Legacy owned pages and any unavailable or changed reference use the ordinary path.

Existing shared bytes are compared before reuse. Staging writes only references for matching content. New content is inserted once. If an existing content key holds damaged bytes, recomputed output uses generation-owned pages instead of overwriting that predecessor. Retirement can then remove the damaged cache safely.

Rows are immutable after insertion. Retirement or generation deletion removes that generation's page references. Shared bytes remain until the last reference disappears. Cancellation rolls back staged writes or discards the candidate without removing the active predecessor's pages. Partial or discarded builds cannot seed reuse.

Shared page content and references participate in the existing storage-change counter. Missing tracking or an unreviewed schema disables validation receipts, including the ADR 0037 staging shortcut. Full stored-graph verification, source-time expiry, and atomic publication remain required.

An edit, late arrival, deletion, changed participant, model/configuration change, or expired source window prevents whole-conversation reuse. A successful page hit may refill the separate message-level cache, within that cache's entry and byte limits. Authorization and independent stored-content verification remain required.

## Qualification

Run `python -m pytest tests/test_conversation_pages.py tests/test_shared_conversation_pages.py tests/test_conversation_graph_references.py tests/test_conversation_graph_receipts.py tests/test_encoded_conversation_graph.py tests/test_small_conversation_pages.py` and the [analytics baseline](qualification.md). Tests include independent full-build comparisons, page boundaries, source mutations, configuration, missing/corrupted pages, staging tamper, budgets, cancellation, expiry and restart.

Measure cold construction and changed-message publication separately. Count conversation-body reads, projector calls, analyzer calls, retained bytes and database writes; do not infer a speedup solely from fewer analyzer calls. Compare identical source and workloads with page reuse available and unavailable. Laptop, installer, production classification and 100,000-message update qualification remain separate gates.

Pages use standard-library compression. Both stored bytes and decompressed output are bounded to 256 KiB. Truncated, trailing or oversized streams are rejected. The existing 64 MiB retention budget counts compressed pages and headers; decoding and assembled outputs consume additional memory.

## Resource use

References charge their complete compressed payload against the 64 MiB cache budget. Page restoration decodes findings, graph IDs and stored properties. It reuses checked graph bytes, but the builder still assembles the complete account graph. The separate analyzer cache also retains copies of its records.
