# ADR 0035: Reuse large conversation outputs in bounded pages

- Status: accepted

## Decision

The compact SQLite pipeline can retain large conversation outputs as bounded pages. Pages contain derived message findings, canonical graph records, and reusable analyzer records. A small header binds them to the exact account, conversation input digest, pipeline configuration, metrics, order, and retained source window.

Each page contains at most 256 records and 256 KiB. A conversation has at most 4,096 pages. Headers and pages share the existing 64 MiB per-build fragment budget and 4,096-conversation limit with ordinary fragments. Reaching a limit requires computation without retaining additional pages; it must not change the answer.

Pages belong to the existing encrypted analytics generation. Only a completed, active predecessor supplies reuse. All pages must be present, ordered and valid before any output contributes to a new build. Invalid or expired pages require computation from current source data. Pages do not contain raw messages or native conversation identifiers.

Staging compares page records with the candidate's actual findings and graph. Checksums alone are not sufficient. Stored graph verification at staging and activation, the canonical witness, source expiry and analysis authorization remain unchanged. Retirement and generation deletion remove the pages.

## Consequences

An unchanged large conversation can skip source loading, analyzer-input construction, metrics calculation and graph projection. Its bounded pages are decoded to rebuild the current account result. If the conversation changes, its pages cannot be reused; the independent message-analyzer cache still applies.

This is whole-conversation output reuse, not within-conversation delta processing. Construction still assembles the logical account graph and message findings. Projection documents, page retention and persisted-content checks remain account-wide costs. Page bounds are not a process-memory guarantee.

The change adds one disposable analytics migration, no database file, runtime package, model or download. Unsupported sources, custom adapters, memory storage and older catalogs preserve their existing paths. ADRs 0027, 0028, 0031, 0032 and 0034 retain their source, verification and publication contracts.

See [Paged conversation reuse](../analytics/conversation-pages.md) for implementation limits and qualification.

Pages use standard-library compression. Both stored bytes and decompressed output are bounded to 256 KiB. Truncated, trailing or oversized streams are rejected. The existing 64 MiB retention budget counts compressed pages and headers; decoding and assembled outputs consume additional memory.
