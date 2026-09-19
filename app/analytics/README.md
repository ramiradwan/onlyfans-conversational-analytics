<!-- CODE-VERIFY: Verify pipeline stages, analyzer behavior, projection storage, graph data boundaries, and module paths against source before editing. -->

# Analytics

Analytics derives metrics, message enrichment, and relationship-graph data from canonical conversation state. It does not own canonical conversation data; analytics output is derived and can be rebuilt.

## Pipeline

For each creator account, the analytics pipeline:

1. reads the current canonical account state;
2. enriches messages through sentiment, topic/entity, and engagement analyzers;
3. calculates conversation and creator metrics;
4. builds a relationship-graph projection;
5. publishes a derived projection only for the matching canonical revision.

Readers use the active projection. Missing, building, and failed projections remain explicit states. Read handlers do not build a projection inline; recovery may be scheduled separately when projection storage is unavailable.

## Data boundaries

- Canonical conversation data remains the source of truth.
- SQLite analytics projections use a separate database from canonical storage.
- The built-in analyzers are deterministic rule-based baselines. Other analyzers can implement the same narrow interfaces.
- Graph projections use opaque references and derived properties instead of copying raw message text into graph properties.
- Projection data may be discarded and rebuilt from canonical state.

## Main modules

- `pipeline.py` — coordinates analytics rebuild and publication.
- `evidence.py` and `evidence_contracts.py` — bounded source-reference lookup through canonical reads. See [Source evidence](../../docs/analytics/evidence.md).
- `enrichment.py` and `analyzers.py` — message enrichment interfaces and built-in analyzers.
- `metrics.py` — conversation and creator metrics.
- `graph_projection.py` and `graph_store.py` — relationship-graph projection and queries.
- `projection_store.py` and `sqlite_projection_store.py` — derived projection storage.
- `rebuild.py` — read-only command-line rebuild from canonical SQLite data.

## Rebuild analytics

See [Rebuild analytics](rebuild.md) for the command-line rebuild procedure.

## Question specifications

[Bounded analytics questions](../../docs/analytics/questions.md) defines source-linked query semantics. [Local analysis](../../docs/analytics/local-analysis.md) defines model and package limits. These specifications do not enable additional endpoints or model downloads.

## Related documentation

- [Brain](../README.md)
- [Proposed analytics scope](../../docs/adr/0013-conversational-analytics-scope.md)
- [Testing](../../docs/testing.md)

## Question execution

`query_contracts.py`, `query_execution.py`, `query_cursor.py`, and `query_service.py` provide typed question plans, bounded read-adapter ports, authenticated pagination, and result validation. See [Execute an analytics question](../../docs/analytics/question-service.md) for composition and adapter requirements.

## Conversation questions

[Question endpoints](../../docs/analytics/question-endpoints.md) connect bounded handlers, witnessed SQLite publication metadata, and exact source resolution. The production adapter reports missing event types as undetermined. Pricing execution remains disabled until its quality gate passes. These routes do not run inference or rebuild projections inline.

## Enrichment reuse

[Reuse unchanged message analysis](../../docs/analytics/enrichment-reuse.md) describes the account-scoped per-analyzer cache, its generation lifetime, context inputs, and size limits. Graph publication remains whole-generation.

## Continuous processing

[Changed-conversation processing](../../docs/analytics/continuous-processing.md) reuses exact conversation outputs, streams canonical identity checks, and reconciles missed work and expiry through the existing scheduler. Publication remains a complete validated generation.

## Generation throughput

[Generation validation and writes](../../docs/analytics/generation-throughput.md) describes scoped integrity checks, owned connections, and bounded adaptive batches.

## Stored publication references

[Bounded publication](../../docs/analytics/bounded-publication.md) describes compact SQLite handoffs, explicit artifact reads, and streaming verification. Full-generation construction and writes remain separate workload costs.

## Compact graph construction

The built-in SQLite path retains encoded graph records between bounded projector batches. See [Compact graph construction](../../docs/analytics/compact-graph.md) for input ownership, verification, and remaining account-wide costs.
