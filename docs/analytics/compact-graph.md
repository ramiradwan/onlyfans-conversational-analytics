<!-- CODE-VERIFY: Check compact_graph.py, conversation_reuse.py, graph_projection.py, pipeline.py, sqlite_projection_store.py, graph_row_encoding.py and test_encoded_conversation_graph.py before changing representation, limits, or validation claims. -->

# Construct compact graph records

The built-in SQLite pipeline encodes graph batches as canonical JSON and releases their node and edge models. A build retains the encoded records, keyed by stable graph identity. Identical shared records are deduplicated; conflicting contents fail the build.

Merging a checked conversation graph reuses its counts and encoded byte total. Only duplicate identities need decoding to subtract their counts and bytes. The merge still visits every record, checks cancellation and rejects conflicting content. Staging independently verifies the stored graph and its counts.

`RelationshipGraphProjector.batches` emits records after each 128 messages and at completion. The full projector collects the same batches for public artifact operations and independent full-build comparisons. Message-order edges span batch boundaries. Shared participants and topic/entity nodes remain logically unchanged.

## Staging and verification

`CompactArtifact` is a private build representation. Public artifact staging rejects it. Its projection retains the full message enrichments and conversation metrics; its graph stores canonical record strings and counts. These strings contain the closed graph fields, not message text.

The build computes its expected graph digest before staging. SQLite staging checks that identity and projection metadata, then inserts sorted records using the existing adaptive write batches and lease session. It does not recreate all graph models or retain a complete SQL-parameter array.

Property triggers, endpoint foreign keys, source identity, durable commits, and cancellation remain required. Staging independently validates and hashes stored rows. Activation reuses an exact-state validation receipt or validates them again. Changing an encoded row between input validation and storage is rejected. The small generation-reference handoff remains unchanged.

Compact builds use [bounded pages](conversation-pages.md) for all conversation sizes when storage supports them. They convert valid full fragments to pages without repeating source reads or analysis. Without page support, a compact build materializes a fragment only for at most 256 messages and half the per-fragment byte limit in graph data.

## Resource limits

The in-process encoded graph still grows with the account. Message enrichments, canonical conversation inputs, encoded projection JSON, and explicit artifact reads also require memory. Compact records reduce Python-object overhead; they do not make whole-account construction constant-memory.

`AnalyticsPipeline(..., compact_graph=False)` retains the object-based path for comparisons without changing the semantic pipeline identity.

## Verification

Run `python -m pytest tests/test_compact_graph.py tests/test_encoded_conversation_graph.py tests/test_continuous_analytics.py tests/test_bounded_publication.py` and the [analytics baseline](qualification.md). Include graph identity, cancellation, tamper, retention, backup, and restart checks. Compare complete artifacts across message-batch boundaries and both graph representations.

Measure cold, forced unchanged, and one-message updates with [the workload tool](continuous-processing.md#verification-and-measurement). Report runtime, observed working-set/private memory, guarded failures, and source/graph equality separately. A completed small synthetic workload does not qualify the constrained-laptop or 100,000-message targets.
