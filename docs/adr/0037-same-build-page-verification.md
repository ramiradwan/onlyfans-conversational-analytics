<!-- CODE-VERIFY: Check conversation_reuse.py, conversation_pages.py, conversation_page_sql.py, sqlite_projection_store.py, validation_receipt.py, sql/0013_shared_conversation_pages.sql, test_conversation_graph_receipts.py and test_shared_conversation_pages.py before changing receipt or fallback claims. -->

# ADR 0037: Reuse same-build page verification during staging

- Status: accepted

## Decision

The built-in compact SQLite path may carry a successful conversation-page restore into staging as a process-local verified page reference. This narrows ADR 0035 only for a page set that the current build already validated completely.

The storage stamp is captured before reading the header and compressed pages. A verified reference is created only after page order, checksums, findings, analyzer records, graph records, endpoints and the conversation graph digest have all passed while that stamp remains unchanged. The receipt includes a process-local MAC over the predecessor generation, complete header and storage stamp.

Staging accepts the reference only inside its write transaction, before its own tracked writes. The same completed active predecessor, exact header and exact tracked storage stamp must still match. The source page set must already use immutable shared page references, and their count must equal the verified header. Staging then copies those generation references without decoding the compressed pages a second time.

A missing, forged or changed receipt, storage stamp, schema, witness, generation, header or shared-reference layout uses ADR 0035's ordinary page reopen and candidate comparison. Legacy generation-owned pages also use that path. The internal verified-reference marker is rejected when supplied directly; a caller-provided digest, receipt or page reference is not sufficient.

## Consequences

The shortcut removes duplicate same-build page decompression and model validation. It does not skip the initial page restore, logical account-graph construction, analyzer-cache validation, complete persisted graph validation, activation checks, source expiry or authorization.

The verified reference is not persisted and cannot survive restart. Retirement and deletion keep the existing shared-page lifetime rules. Future page reads still validate the stored bytes normally.

This decision adds no schema migration, dependency, model, database file or writer process. ADRs 0034, 0035 and 0036 retain their graph-content, page-lifetime and activation-receipt contracts.
