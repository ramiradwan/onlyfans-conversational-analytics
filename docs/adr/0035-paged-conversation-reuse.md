<!-- CODE-VERIFY: Check conversation_reuse.py, conversation_pages.py, conversation_page_sql.py and test_small_conversation_pages.py before editing page eligibility, conversion or limits. -->

# ADR 0035: Reuse conversation outputs in bounded pages

- Status: accepted

## Decision

The compact SQLite pipeline retains conversation outputs as bounded pages when storage supports them, regardless of message count. Pages contain derived message findings, canonical graph records, and reusable analyzer records. A small header binds them to the exact account, conversation input digest, pipeline configuration, metrics, order, and retained source window.

Each page contains at most 256 records and 256 KiB. A conversation has at most 4,096 pages. Headers and pages share the existing 64 MiB per-build fragment budget and 4,096-conversation limit with ordinary fragments. Reaching a limit requires computation without retaining additional pages; it must not change the answer.

Pages belong to the existing encrypted analytics generation. Only a completed, active predecessor supplies reuse. All pages must be present, ordered and valid before any output contributes to a new build. Invalid or expired pages require computation from current source data. Pages do not contain raw messages or native conversation identifiers.

Staging compares page records with the candidate's actual findings and graph. Checksums alone are not sufficient. Stored graph verification at staging and activation, the canonical witness, source expiry and analysis authorization remain unchanged. Retirement and generation deletion remove the pages.

## Consequences

An unchanged conversation can skip source loading, analyzer-input construction, metrics calculation and graph projection. Its bounded pages are decoded to rebuild the current account result. If the conversation changes, its pages cannot be reused; the independent message-analyzer cache still applies.

This is whole-conversation output reuse, not within-conversation delta processing. Construction still assembles the logical account graph and message findings. Projection documents, page retention and persisted-content checks remain account-wide costs. Page bounds are not a process-memory guarantee.

Valid full fragments convert to pages without repeating source reads or analysis. Their original source cutoff and expiry remain binding. Full-model builds and stores without page support use full fragments. Unsupported sources, custom adapters and memory storage use their supported paths. ADRs 0027, 0028, 0031, 0032 and 0034 retain their source, verification and publication contracts.

See [Paged conversation reuse](../analytics/conversation-pages.md) for implementation limits and qualification.

Pages use standard-library compression. Both stored bytes and decompressed output are bounded to 256 KiB. Truncated, trailing or oversized streams are rejected. The existing 64 MiB retention budget counts compressed pages and headers; decoding and assembled outputs consume additional memory.

## Graph-reference encoding

When immutable shared graph storage is available, new pages store graph IDs instead of a second copy of each graph record. The header binds the selected IDs to a digest of the complete conversation graph. Existing self-contained pages remain readable. Disabling shared graph storage converts reused pages back to self-contained records before staging.

Restoration resolves at most 256 IDs at a time through the selected account, generation and graph bucket. It validates actual stored columns and content hashes, then checks the complete conversation graph digest. Staging compares the records with the candidate. The graph-read receipt below can avoid a second source read. A stored digest alone never authorizes reuse. Missing, changed or cross-account records cannot publish.

Conversation reads and page staging use the existing 32 MiB connection-local SQLite cache target. Each scope restores the previous setting, including cancellation and failure. Application cache budgets, source-time expiry, authorization and full persisted-generation verification are unchanged.

This encoding reduces duplicated graph payloads, not logical graph construction. It still restores the whole conversation and assembles the account graph. Reference lookups have a cost; capacity and latency require separate measurements.

## Same-build graph-read receipt

After a complete successful restore, the build may retain a process-local record of the exact generation, page header and tracked storage stamp. The stamp must remain unchanged across the graph read. The record contains no graph payload and is not persisted.

Staging checks that stamp at the start of its write transaction, before its own inserts. An exact match permits checking the stored IDs and complete graph digest against the candidate without reading the source graph again. A missing receipt, changed stamp, schema change or unavailable tracking requires the ordinary row reads. Staging still reopens and checks the page bytes, header and active completed witness.

This receipt does not authorize analysis or replace the independent check of the complete persisted candidate. Changes during the first graph read cannot produce a receipt. Changes before staging invalidate it. Source expiry, account binding, cancellation and publication ownership retain their existing checks.
