# ADR 0013: Define canonical conversational-analytics scope

- Status: proposed

## Context and problem statement

The product preserves and analyzes creator-visible conversations. ADR 0009 makes SQLite canonical and treats graph, search, analytics, and Bridge views as rebuildable local projections; no database, broker, or placeholder enrichment result is itself an analytics authority.

The product needs a precise distinction between observed facts, canonical records, derived analysis, and presentation models. It also needs a first production slice small enough to validate source fidelity before adding broader NLP and graph behavior.

## Decision drivers

- Preserve a source-faithful, explainable local conversation history across historical scrolling and live events.
- Keep raw and canonical facts separate from model-dependent interpretation.
- Make every derived result versioned, replaceable, and rebuildable.
- Keep analytics useful without requiring a cloud database, broker, model API, or graph engine.
- Avoid placeholder values that appear to be real analysis.
- Bound the first production slice around source fidelity and explainable conversation metrics.

## Decision outcome

Choose **canonical local conversation records with versioned enrichment and rebuildable analytics, graph, search, and Bridge projections**.

### Record classes

| Class | Contents | Authority |
| --- | --- | --- |
| Source observations | Accepted snapshots and deltas, stream identity, source sequence, event or snapshot identity, capture time, source payload, and validation outcome | Evidence for replay, deduplication, provenance, and repair |
| Canonical facts | Account-partitioned chats, participants, messages, stable source identifiers, observed text and metadata, direction, timestamps, edit/delete state, ordering evidence, and coverage boundaries | Authoritative local conversation state |
| Derived enrichment | Sentiment, topics, embeddings, engagement-action classifications, outcome classifications, confidence, model and pipeline version, input revision, and processing status | Replaceable interpretation; never a source fact |
| Metrics and read models | Inbox state, counts, trends, response intervals, turns, inactivity, coverage, prioritization inputs, and other query-specific aggregates | Rebuildable projections of canonical facts and selected enrichment |
| Graph and search projections | Relationships, traversal indexes, text indexes, and vector indexes | Optional rebuildable access paths; never the only copy of a fact or result |

### Source fidelity and ordering

- Agent constructs a complete account-scoped snapshot from historical scrolling and live capture, and persists changes before delivery under ADR 0004.
- A snapshot atomically replaces the represented stream at `through_seq`. Later deltas apply as idempotent upserts or deletes. Full replacement remains a recovery and reconciliation mechanism; it is not replaced by blind append-only ingestion.
- Canonical message order preserves every available upstream ordering key. For a snapshot, per-conversation list position is retained as ordering evidence. For a live change, `source_seq` is retained as arrival evidence. An “exact mirror” preserves all available source ordering evidence; it never invents certainty that the source did not provide.
- Only when the upstream source supplies no total-order key may the engine use a documented deterministic fallback from observed timestamp, retained source order, and stable message identity. The resulting order is explicitly marked as inferred with coverage provenance.
- Canonical records retain provenance and coverage sufficient to explain whether a conversation is complete, partial, stale, or inferred. A later authoritative snapshot may correct inferred ordering or earlier content without duplicating a message.
- Enrichment never rewrites observed text, sender, direction, time, identity, or deletion state.

### Enrichment pipeline

1. A canonical commit records durable work for the affected account and canonical revision.
2. A local worker reads a consistent canonical input and records pipeline name, model version, configuration digest, and input revision.
3. Outputs are validated and written as a new derived generation. Missing, unsupported, or failed analysis remains explicitly `unknown` or `unavailable`; fabricated defaults are forbidden.
4. Aggregate, graph, search, and Bridge projections consume only a complete compatible generation.
5. A model or schema change builds a new generation and atomically activates it after validation. Canonical ingestion continues during rebuild.

Enrichment runs locally by default. Adding a remote processor that receives conversation content requires a separate privacy and deployment decision.

### Metrics and read models

Deterministic metrics derive directly from canonical facts and include conversation and message counts, inbound/outbound split, last activity, turn boundaries, response-time distributions, inactivity intervals, and data-coverage indicators. Metric definitions include time zone, interval boundary, population, missing-data, and version semantics.

Model-dependent analytics may include sentiment trends, topic distribution, engagement-action patterns, outcome classifications, similarity, and explainable prioritization. Every displayed value identifies its coverage and derivation version. Observed outcomes and inferred outcomes remain distinct.

Bridge receives a revisioned read model under ADRs 0004 and 0006. The read model is not a second canonical store, and UI fields do not become canonical merely because they are in `state.snapshot` or `state.delta`.

### Graph and search

The graph expresses relationships among canonical conversations, participants, messages, and versioned enrichment results. Search indexes canonical text and selected derived fields. Both are local projection implementations behind interfaces; neither is required for ingest acknowledgment, export, deletion, backup, or recovery.

No particular graph, search, embedding, or NLP engine is selected by this ADR. A specialized local engine may be added only when canonical SQLite remains sufficient to rebuild it and its operational cost is justified.

### First production slice

The first production slice includes:

- source-faithful account-partitioned snapshot/delta reconciliation with retained ordering and coverage evidence;
- canonical chats and messages plus the Inbox conversation/message read model;
- deterministic counts, inbound/outbound split, last activity, turns, response-time distributions, inactivity intervals, and coverage status;
- the versioned enrichment job/result boundary, with truthful unavailable states; and
- export, deletion, backup, and complete projection rebuild from canonical records.

Semantic sentiment and topic models are outside the capture-fidelity first gate. Validated local sentiment and topic processing is the next analytics increment and must supply model/version provenance, quality criteria, coverage semantics, truthful unavailable states, and rebuild tests before activation. Its staging does not remove it from product scope.

Embeddings, recommendations, causal claims, graph exploration, vector search, and automated command proposal generation remain later increments with their own validation and explainability requirements.

### Capability and data-access rules

Capability policy may stop new capture or new derived processing. It does not delete canonical records or disable the work needed to view, export, back up, delete, or rebuild access projections for existing data.

## Consequences

### Positive

- The original conversational-analytics direction remains explicit without making a graph database authoritative.
- Historical and live capture converge through one ordering and replacement model.
- Analytics are explainable and can be recomputed after model or schema changes.
- The first production slice validates data fidelity before model-dependent features expand the product surface.

### Negative

- Source completeness and ambiguous ordering require explicit coverage metadata and tests.
- The initial analytics surface is narrower than the long-term NLP and graph direction.
- Derived generations and provenance consume local storage and require compaction policy.

### Secondary documentation

After acceptance, current model, service, endpoint, and project READMEs must be corrected to remove placeholder analytics, cloud-graph assumptions, and obsolete message names. Follow-up implementation decisions must define canonical ordering fields, coverage representation, metric formulas, enrichment generation storage, and local model quality gates without changing protocol v1 implicitly.

## Confirmation

- Replay tests combine paginated historical capture with interleaved live inbound and outbound changes and converge on one duplicate-free canonical history.
- Snapshot replacement tests correct stale content and ordering without losing post-`through_seq` deltas.
- Ordering tests preserve every available source-order key and permit deterministic inference only when no upstream total-order key exists; inferred results expose coverage provenance and accept later snapshot correction.
- Provenance tests trace every metric and enrichment result to a canonical revision and pipeline version.
- Rebuild tests delete `projections.sqlite3` and reproduce Inbox and deterministic metrics from canonical records.
- Negative tests prove that unavailable enrichment produces no placeholder sentiment, topic, outcome, priority, or graph facts.
- Export and deletion tests operate from canonical facts and remove dependent projections.
- Static and runtime checks find no required external broker, hosted database, hosted model API, or hosted conversation-data path.
