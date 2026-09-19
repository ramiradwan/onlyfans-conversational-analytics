<!-- CODE-VERIFY: Check compact_graph.py, conversation_reuse.py, graph_projection.py, pipeline.py, and sqlite_projection_store.py before changing representation, limits, or validation claims. -->

# Construct compact graph records

The built-in SQLite pipeline encodes graph batches as canonical JSON and releases their node and edge models. A build retains the encoded records, keyed by stable graph identity. Identical shared records are deduplicated; conflicting contents fail the build.

`RelationshipGraphProjector.batches` emits records after each 128 messages and at completion. The full projector collects the same batches for public artifact operations and independent full-build comparisons. Message-order edges span batch boundaries. Shared participants and topic/entity nodes remain logically unchanged.

## Staging and verification

`CompactArtifact` is a private build representation. Public artifact staging rejects it. Its projection retains the full message enrichments and conversation metrics; its graph stores canonical record strings and counts. These strings contain the closed graph fields, not message text.

The build computes its expected graph digest before staging. SQLite staging checks that identity and projection metadata, then inserts sorted records using the existing adaptive write batches and lease session. It does not recreate all graph models or retain a complete SQL-parameter array.

Property triggers, endpoint foreign keys, source identity, durable commits, and cancellation remain required. Persisted rows are independently validated and hashed at staging and activation. Changing an encoded row between input validation and storage is rejected. The small generation-reference handoff remains unchanged.

Compact builds retain a new full conversation fragment only when it has at most 256 messages and its encoded graph fits half the existing per-fragment byte limit. Existing stored fragments still pass their declared limits and source checks. Larger conversations use per-message analyzer reuse instead of materializing another full graph for a fragment cache.

## Resource limits

The in-process encoded graph still grows with the account. Message enrichments, canonical conversation inputs, encoded projection JSON, and explicit artifact reads also require memory. Compact records reduce Python-object overhead; they do not make the whole pipeline constant-memory or avoid writing unchanged rows to a new generation.

No dependency, model, database, or schema migration is added. `AnalyticsPipeline(..., compact_graph=False)` retains the object-based path for comparisons without changing the semantic pipeline identity.

## Verification

Run `python -m pytest tests/test_compact_graph.py tests/test_continuous_analytics.py tests/test_bounded_publication.py` and the [analytics baseline](qualification.md). Include graph identity, cancellation, tamper, retention, backup, and restart checks. Compare complete artifacts across message-batch boundaries and both graph representations.

Measure cold, forced unchanged, and one-message updates with [the workload tool](continuous-processing.md#verification-and-measurement). Report runtime, observed working-set/private memory, guarded failures, and source/graph equality separately. A completed small synthetic workload does not qualify the constrained-laptop or 100,000-message targets.
