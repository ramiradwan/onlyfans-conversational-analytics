<!-- CODE-VERIFY: app/analytics/pipeline.py app/analytics/canonical_source.py app/analytics/historical_derivation.py app/analytics/metrics.py app/analytics/analyzers.py app/analytics/enrichment.py app/analytics/graph_projection.py app/analytics/graph_identity.py app/analytics/opaque_refs.py app/models/analytics.py app/models/insights.py app/analytics/sqlite_projection_store.py app/analytics/sqlite_graph_store.py app/persistence/projection_activation.py -->

# Rebuild equivalence contract

This document defines the research-gate contract for Task 6 of the architectural hardening plan (PR 5R, governing subsequent execution in PR 6A and PR 6B). It is derived from the executable specifications in `app/analytics/pipeline.py`, `app/analytics/canonical_source.py`, `app/analytics/historical_derivation.py`, `app/analytics/metrics.py`, `app/analytics/analyzers.py`, `app/analytics/enrichment.py`, `app/analytics/graph_projection.py`, `app/analytics/graph_identity.py`, `app/analytics/opaque_refs.py`, `app/models/analytics.py`, `app/models/insights.py`, `app/analytics/sqlite_projection_store.py`, `app/analytics/sqlite_graph_store.py`, `app/persistence/projection_activation.py`, and accepted architecture decision records (ADR 0001, ADR 0004, ADR 0006, ADR 0008, ADR 0009, ADR 0010, ADR 0019, ADR 0020, ADR 0021, and ADR 0022). Proposed ADR 0012 is explicitly not promoted or assumed.

The contract establishes the dual-objective invariant definitions, the explicit `ReproducibilityContext` tuple, the derived field taxonomy, the 19-row invariant and oracle matrix, the deletion-focused equivalence profile, permanent oracle falsifier designs, persistent SQLite store qualification requirements, CI shrinking benchmark specifications, and gate blocker criteria that govern implementation in Task 6A (deterministic rebuild oracle) and Task 6B (incremental-versus-clean-rebuild convergence).

## 1. Operating context and authority boundaries

The conversational analytics plane is a disposable derived projection of authoritative canonical persistence. It guarantees two foundational correctness properties:

1. **Property 6A — Derived-State Determinism:**
   Given the same canonical input state and an identical `ReproducibilityContext`, two independent clean builds produce semantically equivalent derived projections and knowledge graphs.
   $$\text{Build}(C, R_1) \equiv_{\text{sem}} \text{Build}(C, R_2) \quad \text{where } R_1 = R_2$$
2. **Property 6B — Incremental-versus-Rebuild Convergence:**
   Given any valid sequence of canonical mutations applied incrementally to live derived stores, a subsequent clean rebuild from the final canonical snapshot under the same `ReproducibilityContext` produces identical semantic state and provenance.
   $$\text{Incremental}(C_0 \xrightarrow{\Delta^*} C_n, R) \equiv_{\text{sem}} \text{Rebuild}(C_n, R)$$

### 1.1 Rationale for the dual-objective split

This two-tier separation is deliberate:
- If **Property 6A** fails, the analytics engine is intrinsically non-deterministic (e.g., relying on unfrozen wall-clock time, non-deterministic graph identifier generation, or unrecorded configuration drift).
- If **Property 6A** passes but **Property 6B** fails, the incremental maintenance logic is defective (e.g., stale caches, missed edge retractions upon message deletion, metric accumulation skew across deltas, or divergent aggregation between streaming and batch paths).

### 1.2 Architectural authority

The analytics plane is strictly subordinate to canonical persistence:
- **Canonical Authority:** `HistoryRepository` (`app/persistence/history.py`) is the sole system of record for account entities and events.
- **Canonical Bridge:** `HistoryAnalyticsSource` (`app/analytics/canonical_source.py`) reads committed, non-deleted canonical entities (`WHERE is_deleted=0`) and provides an immutable, sorted `AccountReadModel`.
- **Derived Authority:** `AnalyticsPipeline` (`app/analytics/pipeline.py`) coordinates enrichment, metric calculation, graph projection, and multi-stage publication into `AnalyticsProjectionStore` and `GraphReader`/`GraphGenerationWriter`.
- **Witness & Activation Authority:** `ProjectionActivationRepository` (`app/persistence/projection_activation.py`) manages canonical publication intents, witness sequences, and CAS activation on canonical storage.
- **Identity Authority:** Opaque reference generation in `app/analytics/opaque_refs.py` and graph identity hashing in `app/analytics/graph_identity.py` define the canonical identifier space. Node and edge identities are stable semantic functions of partition, kind, and canonical identity parts.

## 2. Audited seams and codebase inventory

The following components were audited to construct this contract:

| Component / File | Role in Rebuild Equivalence | Key Classes / Functions |
|---|---|---|
| `app/analytics/pipeline.py` | Central orchestration of build, staging, CAS publication, and retention cutoff | `AnalyticsPipeline`, `ProjectionCandidate`, `PipelineRun`, `rebuild_projection`, `_RETENTION_CUTOFF` |
| `app/analytics/canonical_source.py` | Projection of canonical SQLite tables into `AccountReadModel` | `HistoryAnalyticsSource`, `canonical_content_digest`, `account_read_model` |
| `app/analytics/historical_derivation.py` | 90-day retention cutoff authority and derivation provenance | `HistoricalDerivationProvenance`, `historical_retention_cutoff`, `source_time_is_authorized` |
| `app/analytics/metrics.py` | Deterministic computation of conversation and creator metrics | `build_conversation_metrics`, `build_creator_metrics`, `MetricProvenance` |
| `app/analytics/analyzers.py` | Rule-based deterministic text, sentiment, and topic analyzers | `RuleBasedSentimentAnalyzer`, `RuleBasedTopicEntityAnalyzer`, `RuleBasedEngagementAnalyzer` |
| `app/analytics/enrichment.py` | Multi-stage message enrichment pipeline | `EnrichmentStage`, `MessageEnrichment`, `SentimentResult`, `TopicEntityResult`, `EngagementResult` |
| `app/analytics/graph_projection.py` | Property graph generation from conversations and enrichments | `RelationshipGraphProjector`, `GraphProjectionSummary` |
| `app/analytics/graph_identity.py` | Deterministic namespacing and hashing of graph node and edge IDs | `graph_id`, `require_graph_id` |
| `app/analytics/opaque_refs.py` | Domain-separated SHA-256 opaque references | `opaque_ref`, `account_ref`, `conversation_ref`, `participant_ref`, `message_ref`, `topic_ref`, `entity_ref` |
| `app/models/analytics.py` | Closed Pydantic v2 domain models for derived artifacts | `AnalyticsProjection`, `RebuildArtifact`, `GraphNode`, `GraphEdge`, `ConversationMetrics`, `CreatorMetrics` |
| `app/models/insights.py` | Insight summaries and diagnostic representations | `ConversationInsights`, `CreatorInsights` |
| `app/analytics/sqlite_projection_store.py` | File-backed SQLite projection persistence with generation lifecycle | `SQLiteAnalyticsProjectionStore`, `ProjectionValidationError` |
| `app/analytics/sqlite_graph_store.py` | File-backed SQLite graph repository with referential checks | `SQLiteGraphGenerationWriter`, `SQLiteGraphReader`, `GraphReferentialIntegrityError` |
| `app/persistence/projection_activation.py` | Canonical activation intents and completed publication witnesses | `ProjectionActivationRepository`, `ProjectionActivationIntent`, `ProjectionActivationConflict` |

## 3. ReproducibilityContext definition

Deterministic rebuild equivalence requires an explicit, closed reproducibility context. Derivation outputs are reproducible only when all parameters of this context are identical.

### 3.1 Context tuple specification and implementation mapping

The explicit `ReproducibilityContext` tuple is proposed Task 6 test and oracle vocabulary. Its fields map directly to existing production attributes and parameters as follows:

```python
@dataclass(frozen=True, slots=True)
class ReproducibilityContext:
    canonical_account_ref: str          # Format: a1:[0-9a-f]{64} (existing: account_ref)
    canonical_view_revision: int        # Monotonic canonical sequence head (existing: view_revision)
    canonical_content_digest: str       # Format: sha256:[0-9a-f]{64} over canonical entities (existing: canonical_identity)
    pipeline_revision: str              # Pipeline code revision (existing: AnalyticsPipeline.pipeline_revision)
    pipeline_config_digest: str         # Config digest (existing: AnalyticsPipeline.pipeline_config_digest)
    analyzer_provenance: tuple[tuple[str, str, str], ...] # Sorted (name, revision, digest) (existing: EnrichmentStage.provenance)
    graph_schema_version: str           # Graph schema identifier (proposed explicit parameter; currently in pipeline_config_digest)
    retention_policy: tuple[int, str, str] # (90, "canonical_message_sent_at", "historical-derivation.v1") (existing constants)
    evaluation_clock: datetime          # Frozen timezone-aware UTC datetime (existing: clock parameter to AnalyticsPipeline)
    deterministic_seed: int | None = None # Proposed test harness seed parameter (currently N/A: rule-based deterministic)
    external_enrichment_provenance: tuple[()] = () # Proposed contract parameter (prohibited in baseline; strictly empty)
```

| Context Field | Type / Representation | Current Production Grounding | Status in Codebase |
|---|---|---|---|
| `canonical_account_ref` | `AccountRef` (`a1:[0-9a-f]{64}`) | `opaque_refs.account_ref(creator_account_id)` | Existing production symbol |
| `canonical_view_revision` | `int` ($\ge 0$) | `AccountReadModel.view_revision` / `account_heads.canonical_revision` | Existing production attribute |
| `canonical_content_digest` | `Sha256Digest` (`sha256:[0-9a-f]{64}`) | `canonical_identity(account).content_digest` via `canonical_source.py` | Existing production function |
| `pipeline_revision` | `str` | `AnalyticsPipeline.pipeline_revision` | Existing production property |
| `pipeline_config_digest` | `Sha256Digest` | `AnalyticsPipeline.pipeline_config_digest` | Existing production property |
| `analyzer_provenance` | `tuple[tuple[str, str, str], ...]` | `EnrichmentStage.provenance(enrichments)` | Existing production method |
| `graph_schema_version` | `str` (e.g. `"relationship_graph.v1"`) | Embedded in `pipeline_config_digest` (`"graph_projector"`) | Proposed explicit test field |
| `retention_policy` | `tuple[int, str, str]` | Embedded in `pipeline_config_digest` (`participant_retention_days=90`, `retention_clock`, `historical_derivation_provenance`) | Existing production constants, proposed explicit tuple |
| `evaluation_clock` | `datetime` (aware UTC) | Injected `clock` parameter in `AnalyticsPipeline.__init__` | Existing production parameter |
| `deterministic_seed` | `int | None` | Not applicable (rule-based algorithms are strictly deterministic) | Proposed test harness field |
| `external_enrichment_provenance` | `tuple[()]` | Prohibited in baseline (strictly empty) | Proposed test harness field |

### 3.2 Guarantee scope and boundary conditions

The system guarantees:
$$\begin{aligned}
&\text{Same Build} + \text{Same Revisions} + \text{Same Config Digests} + \text{Same Frozen Clock} + \text{Same Canonical State} \\
&\implies \text{Semantically Equivalent Derived State}
\end{aligned}$$

- **Cross-Version Non-Claim:** The system does not claim cross-version reproducibility across different `pipeline_revision` or `analyzer_revision` versions. A modification to an analyzer or metric formula produces a distinct, traceable configuration digest and pipeline identity digest.
- **Frozen Time Requirement:** Because `AnalyticsPipeline` bounds historical conversations using `historical_retention_cutoff(evaluation_clock)` (retaining only messages where `sent_at > evaluation_clock - 90 days`), an unfrozen clock will cause silent exclusion of messages crossing the 90-day boundary between runs. The evaluation clock must be strictly frozen during equivalence tests.
- **External Model Calls:** Standard analytics derivations must remain purely local, offline, and rule-based. Zero live external API calls or non-deterministic ML inferences are permitted. If external enrichment is introduced, it must be staged as immutable canonical input with explicit provenance.

## 4. Field taxonomy and inventory

Every attribute of derived analytics models (`AnalyticsProjection`, `RebuildArtifact`, `GraphNode`, `GraphEdge`, `ConversationMetrics`, `CreatorMetrics`, `MessageEnrichment`) is classified into one of three exhaustive categories.

```
       Derived Surface Fields
       ├── 1. Semantic State (Exact equality required)
       ├── 2. Provenance (Exact equality required)
       └── 3. Lifecycle / Transient (Intentionally excluded)
```

### 4.1 Field classification inventory

| Model / Scope | Field Name | Classification | Invariant / Comparison Rule |
|---|---|---|---|
| `AnalyticsProjection` | `schema_version` | Provenance | Must equal `"3"` exactly |
| `AnalyticsProjection` | `availability` | Semantic State | Must equal `AvailabilityStatus.AVAILABLE` |
| `AnalyticsProjection` | `pipeline_revision` | Provenance | Exact string equality across builds under same context |
| `AnalyticsProjection` | `pipeline_config_digest` | Provenance | Exact SHA-256 match |
| `AnalyticsProjection` | `pipeline_identity_digest` | Provenance | Exact SHA-256 match (computed from revision + config) |
| `AnalyticsProjection` | `account_ref` | Provenance | Exact `a1:[0-9a-f]{64}` match |
| `AnalyticsProjection` | `source_revision` | Provenance | Exact match with `canonical_view_revision` |
| `AnalyticsProjection` | `projection_generation` | Lifecycle/Transient | **Excluded from semantic equality.** Rebuilds in fresh stores allocate generation 1; incremental stores increment monotonically. |
| `AnalyticsProjection` | `canonical_content_digest` | Provenance | Exact SHA-256 match with canonical snapshot |
| `AnalyticsProjection` | `graph_digest` | Semantic State | Exact SHA-256 match (computed over normalized nodes & edges) |
| `AnalyticsProjection` | `analyzers` | Provenance | Exact list equality of `AnalyzerProvenance` records |
| `AnalyticsProjection` | `window` | Semantic State | Exact equality of `AnalyticsWindow` (scope, start, end) |
| `AnalyticsProjection` | `message_enrichments` | Semantic State | Exact list equality ordered by `(conversation_ref, source_ordinal)` |
| `AnalyticsProjection` | `conversation_metrics` | Semantic State | Exact list equality ordered by `conversation_ref` |
| `AnalyticsProjection` | `creator_metrics` | Semantic State | Exact record equality across all metric values |
| `AnalyticsProjection` | `graph` | Semantic State | Exact equality of `GraphProjectionSummary` counts |
| `AnalyticsProjection` | `projection_digest` | Provenance / Composite | Matches between identical generations; across different generations, compared via semantic digest (excluding generation). |
| `RebuildArtifact` | `nodes` | Semantic State | Set equality of normalized `GraphNode` records |
| `RebuildArtifact` | `edges` | Semantic State | Set equality of normalized `GraphEdge` records |
| `GraphNode` | `node_id` | Semantic State | Stable semantic identity across equivalent builds (currently implemented via `graph_id(...)` in `app/analytics/graph_identity.py`). Requirement is stable semantic identity, not immutable adherence to the helper function. |
| `GraphNode` | `account_ref` | Semantic State | Exact `a1:[0-9a-f]{64}` match |
| `GraphNode` | `kind` | Semantic State | Exact `GraphNodeKind` enum match |
| `GraphNode` | `occurred_at` | Semantic State | Exact UTC ISO-8601 timestamp match |
| `GraphNode` | `properties` | Semantic State | Exact dictionary equality of validated properties |
| `GraphEdge` | `edge_id` | Semantic State | Stable semantic identity across equivalent builds (currently implemented via `graph_id(...)` in `app/analytics/graph_identity.py`). |
| `GraphEdge` | `account_ref` | Semantic State | Exact `a1:[0-9a-f]{64}` match |
| `GraphEdge` | `source_id` | Semantic State | Exact node ID reference match |
| `GraphEdge` | `target_id` | Semantic State | Exact node ID reference match |
| `GraphEdge` | `relation` | Semantic State | Exact `GraphRelation` enum match |
| `GraphEdge` | `occurred_at` | Semantic State | Exact UTC ISO-8601 timestamp match |
| `GraphEdge` | `sequence` | Semantic State | Exact integer sequence match |
| `GraphEdge` | `properties` | Semantic State | Exact dictionary equality of validated properties |
| `ConversationMetrics` | All count fields (`message_count`, etc.) | Semantic State | Exact non-negative integer equality |
| `ConversationMetrics` | Floating-point metrics (`average_response_seconds`, etc.) | Semantic State | Exact IEEE-754 64-bit float equality; tolerance of `1e-9` permitted only if cross-architecture compiler differences occur. |
| `ConversationMetrics` | Distribution maps (`sentiment_counts`, `topic_counts`, etc.) | Semantic State | Exact dictionary equality |
| `CreatorMetrics` | All metric counts and totals | Semantic State | Exact integer / float equality |
| `MessageEnrichment` | `sentiment`, `topic_entities`, `engagement` | Semantic State | Exact equality of nested analyzer result models |
| Store Publication Metadata | `publication_epoch` | Lifecycle/Transient | **Excluded.** Transient epoch string assigned on store open. |
| Store Publication Metadata | `staged_generation_id` | Lifecycle/Transient | **Excluded.** UUID allocated during staging. |
| Store Publication Metadata | `owner_id`, `owner_pid`, `lease_expires_at` | Lifecycle/Transient | **Excluded.** Transient lock ownership data. |
| Store Publication Metadata | `started_at`, `validated_at`, `activated_at` | Lifecycle/Transient | **Excluded.** Wall-clock execution timestamps. |

## 5. Invariant and oracle matrix

The Task 6 test harness implements nineteen mandatory testable invariants, split across Task 6A (derived-state determinism) and Task 6B (incremental-versus-rebuild convergence).

| # | Invariant | Testable Assertion | Verification Method | Falsifier Trigger |
|---|---|---|---|---|
| 1 | **Canonical witness** | `candidate.source_revision == account.view_revision` and `projection.source_revision == account.view_revision` | Assert equality of source revision in projection candidate and active store against canonical head. | `BrokenProvenanceAdapter`: alters source revision in candidate or publishes against stale revision. |
| 2 | **Canonical digest** | `projection.canonical_content_digest == canonical_identity(account).content_digest` | Compute SHA-256 digest over canonical account state; verify exact match in projection metadata. | `BrokenProvenanceAdapter`: computes digest over partial canonical state or corrupts digest string. |
| 3 | **Pipeline provenance** | `projection.pipeline_revision == ctx.pipeline_revision` and `projection.pipeline_config_digest == ctx.pipeline_config_digest` | Verify pipeline code revision and configuration hash match execution context exactly. | Invalidate config parameter; verify pipeline identity digest changes. |
| 4 | **Analyzer provenance** | `projection.analyzers == sorted(ctx.analyzer_provenance)` | Verify every analyzer name, revision, and config digest matches expected analyzer set. | Alter analyzer configuration or revision; assert mismatch detected. |
| 5 | **Message identity** | For all messages $m \in \text{account}$, `message_ref(acc, conv, m.id)` is invariant across builds. | Verify message reference mapping remains identical across retry, rebuild, and incremental runs. | `BrokenIdentityAdapter`: introduces non-deterministic random salts or sequence-dependent message IDs. |
| 6 | **Participant identity** | Participant opaque refs `p1:...` remain identical regardless of ingestion order or chat sequence. | Reorder chat arrival order in canonical store; verify participant references do not change. | Reordering participant discovery flips participant IDs. |
| 7 | **Conversation identity** | Conversation opaque refs `c1:...` map deterministically from `(creator_account_id, conversation_id)`. | Assert exact conversation ref match across clean rebuilds and incremental paths. | `BrokenIdentityAdapter`: modifies conversation hash logic. |
| 8 | **Metric equivalence** | `norm(rebuilt.conversation_metrics) == norm(incremental.conversation_metrics)` and `rebuilt.creator_metrics == incremental.creator_metrics` | Compare metric maps keyed by semantic conversation reference and account reference. | `BrokenAnalyticsAdapter`: introduces delta-drift in response latency or turn count calculation. |
| 9 | **Enrichment equivalence** | `norm(rebuilt.message_enrichments) == norm(incremental.message_enrichments)` | Compare enrichment lists sorted by `(conversation_ref, source_ordinal)`. | Non-deterministic tie-breaking in topic or sentiment classification. |
| 10 | **Topic/entity identity** | Topic refs `t1:...` and entity refs `x1:...` remain invariant for identical taxonomy and normalized values. | Assert topic and entity opaque refs are stable across builds. | Change entity normalization rule; verify hash mismatch. |
| 11 | **Graph nodes** | `set(rebuilt.nodes) == set(incremental.nodes)` | Compare normalized `(node_id, kind, properties)` sets from graph reader across builds. | `BrokenAnalyticsAdapter`: omits or alters node properties on incremental path. |
| 12 | **Graph edges** | `set(rebuilt.edges) == set(incremental.edges)` | Compare normalized `(edge_id, source_id, relation, target_id, properties)` sets. | `BrokenAnalyticsAdapter`: omits one graph edge on incremental path. |
| 13 | **Graph identity** | Stable semantic identity: for all nodes and edges, IDs are generated deterministically from partition, kind, and canonical identity components without stateful or execution-order counters. | Assert node and edge IDs are identical between clean builds from the same canonical state. | `BrokenIdentityAdapter`: uses auto-incrementing row IDs instead of stable semantic identity. |
| 14 | **Graph digest** | `rebuilt.projection.graph_digest == incremental.projection.graph_digest` | Verify SHA-256 graph digest over sorted canonical node/edge serialization matches. | Inject extraneous whitespace or order variation in graph digest computation. |
| 15 | **Referential closure** | $\forall e \in \text{edges}: e.\text{source\_id} \in \text{nodes} \land e.\text{target\_id} \in \text{nodes}$ | Verify graph store raises `GraphReferentialIntegrityError` if dangling edges are inserted. | `BrokenAnalyticsAdapter`: inserts edge pointing to non-existent node; assert rejection. |
| 16 | **Publication freshness** | Active generation in store is updated if and only if canonical witness check (`reserve`, `complete`, and CAS on `projection_generations.status='validated'`) succeeds. | Attempt publication with stale canonical revision or digest; assert `ProjectionActivationConflict` or `CanonicalRevisionChanged` is raised. | Stale worker publication attempt after newer canonical mutation succeeds. |
| 17 | **Retry/rebuild metamorphism** | Re-running clean rebuild or applying redundant idempotent deltas yields semantically identical projection. | Build projection, apply redundant deltas, re-project; verify `changed == False` and projection is identical. | Incremental pipeline mutates projection on idempotent delta. |
| 18 | **Clock determinism** | Rebuild with identical frozen `evaluation_clock` yields identical historical message retention window. | Rebuild twice with same frozen clock; verify retained message count and cutoff match. | Advance clock across 90-day boundary; verify message retention changes. |
| 19 | **Config sensitivity** | Altering analyzer or pipeline configuration produces distinct `pipeline_config_digest` and invalidates cached projection. | Modify configuration parameter; assert cached projection is rejected and rebuild is forced. Config sensitivity requires explicit pipeline/analyzer revision/config provenance; arbitrary code edits do not automatically alter a digest without updating declared revision/configuration. | Cache hit occurs despite modified analyzer configuration. |

## 6. Deletion-focused equivalence profile

Deletion handling is the primary source of incremental-versus-rebuild divergence in derived analytics. A dedicated deletion-focused equivalence profile must be maintained in the test suite.

### 6.1 Lifecycle sequence for deletion equivalence

The test harness exercises the following mandatory adversarial sequence:
```
[Create Canonical Entities] ──> [Incremental Analytics Update] ──> [Assert Derived State Present]
                                                                             │
                                                                             ▼
[Clean Rebuild from Post-Delete] <── [Assert Referential Closure] <── [Tombstone / Soft-Delete]
                │                                                            │
                ▼                                                            ▼
[Compare Incremental vs Clean] <────────────────────────────── [Replay Stale Retries / Delays]
```

### 6.2 Strict absence assertions

Following a canonical deletion event (chat tombstone, message deletion, or account purge), the test harness asserts strict absence of deleted material from all derived surfaces:

1. **Metrics Exclusion:**
   - Deleted messages must not contribute to `message_count`, `turn_count`, `response_opportunity_count`, `responded_count`, or spend metrics in either `ConversationMetrics` or `CreatorMetrics`.
   - If an entire conversation is tombstoned, its `ConversationMetrics` record must be completely absent from `AnalyticsProjection.conversation_metrics`.
2. **Enrichment Exclusion:**
   - Zero `MessageEnrichment` records with `message_ref` corresponding to deleted messages may appear in `AnalyticsProjection.message_enrichments`.
3. **Graph Retraction:**
   - Message nodes corresponding to deleted messages must be absent from `nodes`.
   - If a participant has no remaining non-deleted messages or conversations, the participant node must be retracted.
   - All incident edges (`SENT`, `RECEIVED_BY`, `CONTAINS`, `PRECEDES`, `EXPRESSES_AFFECT`, `HAS_ENGAGEMENT_STATE`, `MENTIONS_TOPIC`, `MENTIONS_ENTITY`) must be retracted.
4. **Referential Closure:**
   - No remaining edge in the graph may reference a retracted node ID. The graph store must enforce referential integrity and reject dangling edges with `GraphReferentialIntegrityError`.
5. **Active Publication:**
   - Publication CAS ensures that the active projection generation reflects only the post-deletion canonical state. Stale cached projections must be invalidated or superseded.

## 7. Permanent oracle falsifiers

To prevent test suite regression and prove that the Task 6 oracle has real fault-detection power, four permanent, deliberately broken test doubles must be implemented in the test harness.

```
       Task 6 Permanent Falsifiers
       ├── BrokenAnalyticsAdapter (omits one edge on incremental path)
       ├── BrokenProvenanceAdapter (publishes under incorrect digest/revision)
       ├── BrokenIdentityAdapter (introduces non-deterministic graph/message IDs)
       └── BrokenDerivedDeletionAdapter (leaves tombstoned material in derived state)
```

### 7.1 Falsifier specifications

1. **`BrokenAnalyticsAdapter`:**
   - *Fault Injected:* Omits exactly one semantic graph edge (or introduces an extraneous edge) only on the incremental update path, while producing correct output on clean rebuild.
   - *Target Invariant:* Invariant 12 (Graph edges) and Invariant 14 (Graph digest).
   - *Expected Oracle Behavior:* Task 6B convergence suite fails with assertion error highlighting the missing/extraneous edge.
2. **`BrokenProvenanceAdapter`:**
   - *Fault Injected:* Publishes derived projection candidates with a manipulated `canonical_content_digest` or stale `source_revision`.
   - *Target Invariant:* Invariant 1 (Canonical witness), Invariant 2 (Canonical digest), and Invariant 16 (Publication freshness).
   - *Expected Oracle Behavior:* Pipeline publication rejects candidate with `ProjectionActivationConflict` or `CanonicalRevisionChanged`, or harness detects digest mismatch.
3. **`BrokenIdentityAdapter`:**
   - *Fault Injected:* Generates node IDs or edge IDs using process-local sequence counters or non-canonical dictionary iteration order rather than stable semantic identity functions.
   - *Target Invariant:* Invariant 5 (Message identity) and Invariant 13 (Graph identity).
   - *Expected Oracle Behavior:* Task 6A determinism suite fails because two clean builds from the same input yield different node/edge IDs.
4. **`BrokenDerivedDeletionAdapter`:**
   - *Fault Injected:* Upon canonical message or chat deletion, correctly updates metrics but leaves the deleted message node or its `PRECEDES` edge in the graph store.
   - *Target Invariant:* Invariant 11 (Graph nodes), Invariant 12 (Graph edges), Invariant 15 (Referential closure), and Deletion Equivalence.
   - *Expected Oracle Behavior:* Deletion-focused profile fails with referential integrity violation or set difference indicating lingering deleted nodes.

### 7.2 Execution rule

These falsifiers are implemented as dedicated test doubles under `tests/hardening/falsifiers/` (or equivalent test directory). **Production source code must never be mutated in CI to achieve falsification.**

## 8. Persistent SQLite store qualification

Before declaring file-backed SQLite stores (`app/analytics/sqlite_projection_store.py` and `app/analytics/sqlite_graph_store.py`) production-equivalent in Tier B testing, their runtime parameters and durability behaviors must be qualified.

### 8.1 SQLite configuration profile

The file-backed analytics projection and graph stores inherit the accepted production profile from `app/persistence/database.py`:

```text
Database Class:     ProjectionsDatabase -> ProjectionsSQLite -> LocalSQLite
Pragmas Applied:
  PRAGMA journal_mode = WAL;
  PRAGMA synchronous = FULL;
  PRAGMA foreign_keys = ON;
  PRAGMA busy_timeout = 5000;
Key Scope:          "analytics-projection" (SQLCipher encrypted via LocalSQLite)
Connection Model:   Tracked connection factory (_TrackedConnection) with in-process lifecycle locks
Writer Model:       SQLite BEGIN IMMEDIATE writer serialization across check_same_thread=False connections
Integrity Checks:   PRAGMA integrity_check == "ok"; PRAGMA foreign_key_check empty
```

### 8.2 Publication lifecycle and atomic witness protocol

Publication of a rebuild artifact into the file-backed store does not update an in-place metadata row via naive SQL. It follows a multi-stage lifecycle protocol across two SQLite databases:

1. **Stage Inactive Generation:** `SQLiteAnalyticsProjectionStore.stage_artifact` persists a new generation record into `projection_generations` with status `'building'`, validates artifact shape and digests, populates `safe_nodes` and `safe_edges` via `SQLiteGraphGenerationWriter`, and transitions status to `'validated'`.
2. **Canonical Witness Reservation:** `publish_generation` validates that the generation identity matches the current canonical identity via `canonical_identity_reader(creator_account_id)`. It calls `ProjectionActivationRepository.reserve(...)` on the authoritative canonical database, creating a `ProjectionActivationIntent` with state `'reserved'`, recording `witness_sequence`, `generation_id`, `canonical_revision`, and `canonical_content_digest`.
3. **Transition to Activation Pending:** The store updates `projection_generations` in the projections database:
   ```sql
   UPDATE projection_generations
      SET status = 'activation_pending',
          activation_intent_id = :intent_id,
          witness_sequence = :witness_sequence,
          lease_expires_at = :lease_expires_at
    WHERE generation_id = :generation_id
      AND status = 'validated'
      AND owner_id = :owner_id AND owner_pid = :owner_pid
      AND owner_process_started_at = :owner_process_started_at
      AND owner_instance_nonce = :owner_instance_nonce
      AND owner_capability_digest = :owner_capability_digest;
   ```
   If ownership differs or the generation is no longer in `'validated'` status, zero rows are updated and the store raises `ProjectionActivationConflict("generation ownership differs")`.
4. **Canonical Intent Completion:** `publish_generation` verifies that the observed generation matches the reserved intent. If so, it calls `ProjectionActivationRepository.complete(intent)`, which atomically marks the intent as `'completed'` in canonical persistence.
5. **Activation of Completed Generation:** `_activate_completed_generation` marks the completed generation as `'active'` and retires any previously active generation (`status = 'retired'`).
6. **Reconciliation on Failure:** If any failure occurs after canonical completion, `ProjectionActivationRepository.reconcile_completed` is called and the generation is retired, preventing inconsistent state between canonical and projection databases.

Exceptions: Stale canonical revision or content digest mismatches raise `ProjectionActivationConflict` or `CanonicalRevisionChanged`. Corrupted row reproduction raises `ProjectionValidationError` or `ProjectionReconciliationError`.

### 8.3 Upstream SQLite durability advisory research and qualification status

- **Dated Local Observation (2026-09-09):** Local environment probe records Python 3.13.6, `sqlcipher3==0.6.2`, SQLite 3.51.1, and SQLCipher 4.12.0 community.
- **Upstream Advisory Evaluation:**
  - The official SQLite WAL-reset advisory identifies a database corruption risk affecting versions 3.7.0 through 3.51.2, fixed in SQLite 3.51.3 (with backports 3.44.6 and 3.50.7). Sources: [SQLite WAL Reset Bug](https://sqlite.org/wal.html#walresetbug) and [SQLite Release 3.51.3](https://sqlite.org/releaselog/3_51_3.html).
  - The failure trigger requires WAL mode combined with two or more connections to the same database across separate threads or processes exhibiting concurrent write and checkpoint behavior.
  - SQLCipher 4.14.0 incorporates SQLite 3.51.3 and strongly recommends that all WAL-mode applications upgrade: [SQLCipher 4.14.0 Release](https://github.com/sqlcipher/sqlcipher/releases/tag/v4.14.0).
  - In the current codebase, `app/persistence/database.py` enforces WAL mode (`PRAGMA journal_mode = WAL`) and synchronous FULL (`PRAGMA synchronous = FULL`), opening fresh `check_same_thread=False` connections per read or transaction. In-process `_connection_locks` serialize native open/close transitions and exclusive lifecycle changes, but individual transaction `BEGIN IMMEDIATE` and commit operations execute outside those locks. While SQLite `BEGIN IMMEDIATE` provides writer serialization, multiple concurrent application connections/threads and active SQLite checkpoint behavior remain relevant and unexcluded.
  - Consequently, the local development profile cannot be qualified as unaffected by the upstream advisory. (No claim is made that corruption has occurred).
- **Production Qualification Requirement:**
  - Actual native runtime versions on PR runners and packaged Windows Store binaries must be measured dynamically during Task 5C / PR 5B.
  - Declaring file-backed Tier B storage production-equivalent remains blocked until an upgrade to a fixed runtime (SQLite $\ge 3.51.3$ / SQLCipher $\ge 4.14.0$) and/or a rigorous proof of trigger exclusion is established.
  - Specific version numbers and advisory ranges are dated observations and must not be encoded as timeless architecture policy; current upstream advisories must be re-evaluated against the deployed runtime at implementation time.

## 9. CI shrinking benchmark design

Task 6 generated property-based tests (Hypothesis) must operate within strict time and resource budgets to ensure PR CI remains fast and responsive.

### 9.1 Named benchmark profiles

Repository-owned named profiles must be used instead of library defaults. These counts represent **initial calibration starting points**, not permanent normative values:

| Profile Name | Target Suite | Starting Examples | Canonical Mutations / Example | Clean Rebuild Passes | Backend | Purpose |
|---|---|---:|---:|---:|---|---|
| `task6a_determinism_fast` | Task 6A | 30 | 12–15 | 2 clean builds / example | In-memory | Broad determinism exploration |
| `task6b_convergence_fast` | Task 6B | 30 | 12–15 | 2–4 rebuilds / example | In-memory | Incremental / clean rebuild convergence |
| `task6_deletion_fast` | Deletion Equivalence | 20 | 10–12 | Post-delete + final rebuild | In-memory | Deletion retraction & referential closure |
| `task6_persistent_tier_b` | Tasks 6A + 6B | 10 | 8–10 | Rebuild + reopen verification | File SQLite | Persistent store & restart qualification |

### 9.2 CI performance budget and response measure

- **Critical-Path Budget:** Tasks 5 and 6 combined must add $\le 5\%$ to the p95 PR critical-path duration.
- **Hard Review Threshold:** Any addition $> 10\%$ requires an explicit architectural performance trade-off review. If budget pressure arises, the example count must be scaled down before reducing transition coverage or removing invariant assertions.
- **Provisional Historical Baseline:** A provisional sample of 15 first-attempt successful PR workflow runs from 2026-09-02 through 2026-09-04 exhibited a p95 elapsed duration of 1704.7 seconds, with Windows execution on the critical path (only 6 of these runs matched the exact current workflow revision; this is provisional historical context, not closure evidence).
- **Shrinking Benchmark:** The benchmark harness must measure both clean passing runs and deliberately failing runs (using falsifiers) to benchmark Hypothesis shrinking duration and reproducer trace minimization.
- **Dynamic Version Measurement:** Python version, SQLite runtime version, SQLCipher version, and Hypothesis version must be captured dynamically from the executing environment rather than hard-coded into architecture contracts.

## 10. Normative discrepancy ledger and gate readiness ledger

### 10.1 Identified normative discrepancies

| # | Seam / File | Observed Code Behavior | Normative Architecture Requirement | Hardening Plan Disposition |
|---|---|---|---|---|
| D01 | `app/analytics/canonical_source.py` | `HistoryAnalyticsSource` imports `AccountReadModel` directly from `app.transport.ingestion`. | Ingestion models belong to transport; canonical read model should be canonically owned without transport dependency. | Documented as current-design exception. Remediated in Task 7C (canonical ownership of `AccountReadModel`). |
| D02 | `app/transport/manager.py` | `InMemoryTransportManager` receives `repositories.ingestion` (an instance of `HistoryAnalyticsSource`) constructed by `create_canonical_repositories` in `app/persistence/factory.py`. | Transport layer should not receive or hold analytics read sources. The canonical analytics adapter should be composed separately by bootstrap. | Documented as current-design exception. Remediated in Task 7B (bootstrap-owned canonical analytics adapter composition). |
| D03 | `app/analytics/pipeline.py` | `AnalyticsPipeline.__init__` accepts `clock: Callable[[], datetime] = utc_now`. When omitted, derivation uses wall-clock time for 90-day retention cutoff. | Rebuild determinism requires identical time bounds. Rebuilds without explicit frozen clock risk non-deterministic message exclusion at 90-day boundary. | Rebuild contract mandates that `ReproducibilityContext.evaluation_clock` be explicitly passed to `AnalyticsPipeline` in all equivalence and determinism tests. |
| D04 | `app/analytics/metrics.py` | Division-by-zero handling in metrics calculators (e.g. `response_coverage`, `average_response_seconds`) returns `None` or `0.0`. | Derived metrics must have strict deterministic sentinels to avoid IEEE-754 NaN/inf values. | `AnalyticsModel` forbids `allow_inf_nan=False`. Contract codifies `None` as absent indicator and `0.0` as zero value. |

### 10.2 PR 5R research gate closure criteria

PR 5R is a research and specification gate. It closes when:

1. [x] **Transition Catalogue Structure & Completeness:** Ingestion transition catalogue (S01–S03, D01–D09, A01–A14, N01–N20, AG01–AG08, exactly 54 entries) is established with all required specification fields present.
2. [x] **Rebuild Equivalence Contract Specification:** Rebuild equivalence contract established with explicit `ReproducibilityContext` (distinguishing existing vs proposed fields), derived field taxonomy, 19-row invariant matrix, deletion profile, and permanent falsifiers.
3. [x] **Zero Documentation & Style Violations:** Documentation checks (`tools/check_docs.py`) pass cleanly on all Markdown files.
4. [x] **Static & Structural Contract Verification:** Static and structural verification tooling confirms that cited file paths, selected AST symbols, tables, and schema constants exist in the codebase, required contract structures and counts are present, and banned fabricated patterns are absent. (Full semantic and source truth requires human architectural review and downstream executable Task 5 and Task 6 oracles; static checks do not assert complete semantic equivalence).
5. [x] **SQLite Durability Advisory Evaluation:** Dated advisory evaluation (2026-09-09) and qualification requirements documented.

### 10.3 Downstream implementation and qualification readiness ledger

The following deliverables are downstream tasks authorized by PR 5R; they do not block PR 5R research gate closure:

- **Task 6A (PR 6A) Readiness:** Deterministic rebuild oracle (`tests/stateful/test_analytics_determinism.py`, `tests/state_models/analytics_oracle.py`).
- **Task 6B (PR 6B) Readiness:** Incremental-versus-clean-rebuild convergence suite (`tests/stateful/test_analytics_equivalence.py`, falsifiers).
- **Task 5A (PR 5A) Readiness:** Brain independent reference model and Tier A state machine.
- **Task 5C (PR 5B) Readiness:** File-backed persistent Brain qualification and benchmark calibration.
- **Packaged Runtime Qualification:** Required before declaring file-backed Tier B storage production-equivalent in Task 5C / PR 5B and closing Task 9; does not block Task 5A or Task 6A design.
