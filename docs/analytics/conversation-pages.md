<!-- CODE-VERIFY: Check conversation_pages.py, conversation_page_sql.py, conversation_reuse.py, conversation_sql.py, sqlite_projection_store.py, sql/0011_conversation_pages.sql and test_conversation_pages.py before editing behavior or limits. -->

# Reuse large conversation results

The compact SQLite path stores large conversation results in pages rather than requiring a complete model-based fragment. This avoids loading and re-projecting an unchanged large conversation during the next admitted build. It does not change public question or graph meanings.

## Bounds and storage

A page holds at most 256 records and 256 KiB. A conversation may use at most 4,096 pages. The header and pages count toward the existing 64 MiB per-build fragment limit and 4,096-conversation limit; ordinary fragments share those limits. Oversized records or exhausted capacity prevent retention, not analysis.

`conversation_page_sets` holds the header. `conversation_pages` holds ordered bytes in the same encrypted analytics database. The header covers input and configuration identity, metrics, expiry, counts and the ordered-page checksum. Pages contain findings, graph records or analyzer reuse records. They do not copy source text or native message identifiers.

Only a completed, active generation supplies pages. Missing, malformed, misordered, incompatible or expired pages cause a source rebuild. Parsing is limited to one page at a time, but the restored conversation output and assembled account graph are still retained. These limits are not a total memory cap.

## Correctness and lifetime

Staging compares every retained record with its candidate artifact, including analyzer results and graph endpoints. Rows are immutable after insertion. Retirement or generation deletion removes the page set and its children. Partial or discarded builds cannot seed reuse.

An edit, late arrival, deletion, changed participant, model/configuration change, or expired source window prevents whole-conversation reuse. Analyzer records preserved in a successful page hit remain available to the separate message-level cache on later edits. Authorization and independent stored-content verification remain required.

## Qualification

Run `python -m pytest tests/test_conversation_pages.py` and the [analytics baseline](qualification.md). Tests include independent full-build comparisons, page boundaries, source mutations, configuration, missing/corrupted pages, staging tamper, budgets, cancellation, expiry and restart.

Measure cold construction and changed-message publication separately. Count conversation-body reads, projector calls, analyzer calls, retained bytes and database writes; do not infer a speedup solely from fewer analyzer calls. Compare identical source and workloads with page reuse available and unavailable. Laptop, installer, production classification and 100,000-message update qualification remain separate gates.

Pages use standard-library compression. Both stored bytes and decompressed output are bounded to 256 KiB. Truncated, trailing or oversized streams are rejected. The existing 64 MiB retention budget counts compressed pages and headers; decoding and assembled outputs consume additional memory.
