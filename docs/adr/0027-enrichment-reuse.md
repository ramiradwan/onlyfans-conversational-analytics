# ADR 0027: Reuse versioned analyzer results within analytics generations

- Status: accepted
- Date: 2026-09-19

## Decision

Keep reusable analyzer results in the existing encrypted analytics projection store. They are optional derived records, not canonical facts or a new database. Memory-backed tests use the same generation boundary.

Each record belongs to one account, message, analyzer, and input definition. Its key includes the complete message input, analyzer revision and configuration, declared model artifact, taxonomy revision, and ordered context inputs. Account revisions and source ordinals are not analyzer inputs and do not invalidate independent message results.

An analyzer must explicitly declare its input policy before its results can be reused. Context-aware analyzers receive the declared bounded window through a separate method. Undeclared analyzers always run. Model adapters must identify their model artifact and cover tokenizer and other inference settings in their configuration.

Write reuse records while staging the projection generation. Only the active generation with an exact completed publication witness supplies hits. Live canonical inputs must reproduce the record key. A changed account revision does not make an old public projection readable; reuse happens only inside a new admitted build.

Retiring or deleting a generation removes its reuse records. Source-time expiry remains authoritative, including all context dependencies. Failed or cancelled builds do not supply reusable output. Restart can reuse a still-witnessed active generation; recovery that retires it produces a cold cache.

## Consequences

Analyzer reuse avoids repeated calls inside changed conversations. [Conversation-level reuse](0028-conversation-incremental-processing.md) supplies unchanged metrics and local graph records. Publication remains a whole-generation operation, and exact canonical identity still requires content scans.

Missing, evicted, malformed, or incompatible records require analysis again. Retained output is stable for the same declared inputs. This is not a promise to reproduce a lost generative-model sample after the projection store is discarded.

Per-build size limits bound retained reuse records. These limits are not a total process-memory guarantee. The implementation adds no model weights, runtime dependencies, inference process, or network traffic.

See [Enrichment reuse](../analytics/enrichment-reuse.md) for the input contract, limits, and qualification commands. The authority, publication, encryption, and retention requirements in ADRs 0009, 0019, 0020, and 0026 remain in force.
