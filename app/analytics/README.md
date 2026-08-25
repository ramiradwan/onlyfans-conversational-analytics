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
- `enrichment.py` and `analyzers.py` — message enrichment interfaces and built-in analyzers.
- `metrics.py` — conversation and creator metrics.
- `graph_projection.py` and `graph_store.py` — relationship-graph projection and queries.
- `projection_store.py` and `sqlite_projection_store.py` — derived projection storage.
- `rebuild.py` — read-only command-line rebuild from canonical SQLite data.

## Rebuild analytics

See [Rebuild analytics](rebuild.md) for the command-line rebuild procedure.

## Related documentation

- [Brain](../README.md)
- [Proposed analytics scope](../../docs/adr/0013-conversational-analytics-scope.md)
- [Testing](../../docs/testing.md)
