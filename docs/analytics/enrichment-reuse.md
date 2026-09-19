<!-- CODE-VERIFY: Check enrichment.py, enrichment_cache.py, enrichment_inputs.py, enrichment_sql.py, pipeline.py, both projection stores, and sql/0005_enrichment_reuse.sql before changing behavior or limit claims. -->

# Reuse unchanged message analysis

The analytics pipeline reuses individual sentiment, topic/entity, and engagement results when their declared inputs match. Changed conversations rebuild their metrics and local graph; [conversation-level reuse](continuous-processing.md) supplies unchanged parts. The pipeline publishes one coherent generation.

## Inputs and adapter identity

`AnalyzerCachePolicy` opts an adapter into reuse. Its descriptor supplies the analyzer name, revision, configuration digest, analysis mode, and calibration status. The policy adds the taxonomy and context revisions, optional model digest, and counts of preceding and following messages.

The key hashes every field in `MessageAnalysisInput`: account, conversation, participant, message, text, UTC timestamp, and direction. It also hashes the ordered context window. Source revision, message ordinal, and rebuild time are excluded. A cache hit receives the current ordinal and source metadata when its `MessageEnrichment` is assembled.

An unrelated new message does not invalidate message-only results. An edit changes that message's key and any context keys that depend on it. A deletion removes it from the build; changed context windows are recomputed. A different model, taxonomy, context policy, or analyzer configuration invalidates only that analyzer's results.

Context counts are bounded to 32 messages on each side. The adapter must implement `analyze_with_context(message, context)` when either count is nonzero. Context excludes the target and follows the pipeline's retained-message order. Both cold and cached execution receive identical inputs. The input policy cannot change after pipeline construction.

Adapters without a policy are not cached. A model-mode adapter must declare a model digest. The adapter's configuration must include every other output-affecting input, including tokenizer, inference settings, and custom code revision. Undeclared external state is not supported.

## Storage and lifetime

`enrichment_reuse` is generation-scoped in `analytics-projections.sqlite3`. It contains opaque references, input/configuration digests, expiry, validated analyzer results, and a checksum. It does not copy input message text or native identifiers. Results are the same typed values already present in the projection.

Staging validates each record against its projection result. SQL allows insertion only while that generation is building and blocks updates. Records are never public query results. A lookup requires the active generation's completed canonical witness, then an exact current input key. A missing witness or malformed record gives a miss, not a substitute result.

Retirement, deletion, clear, and expiry remove generation cache records. No independent durable cache, cleanup process, or backup path is added. Reopening a valid store can reuse its active generation. Retired or discarded generations cannot seed reuse, even after a restart.

The earliest source time among the target and all context messages determines expiry. A lookup cannot restart that period. An admitted build is still required even when every analyzer call can be skipped. The cache does not grant analysis authority.

## Checked record staging

An unchanged conversation retains its checked serialized analyzer records without rebuilding the result objects. Each record's key is computed once during retention. Staging still parses the record and compares its result, account, conversation and expiry with the current projection.

SQLite staging validates one record at a time and prepares immutable scalar fields for insertion. It writes at most 64 records per batch inside the existing candidate transaction. Invalid input, cancellation, or an insertion failure rolls back the transaction, including earlier batches. The cache creates no separate transaction or writer. The memory backend uses the same validation rules.

Batches can hold up to 4 MiB of serialized documents in addition to the existing 16 MiB retained-input budget, plus Python objects. This is not a total memory cap. Unchanged cache records are still physically copied into the new generation; this change reduces preparation and SQL-call overhead rather than introducing shared cache storage.

## Limits and fallback

The pipeline retains at most 30,000 cache records and 16 MiB of serialized cache data per build. Each record is at most 64 KiB. It prepares at most 64 messages per lookup batch, with three analyzer keys per message. SQL lookups use groups of at most 64 keys.

When a cache limit is reached, analysis continues without retaining more records. Eviction can increase later analyzer calls but must not change the permitted answer. Input arrays, context objects, projection records, and the graph consume additional memory; these cache limits are not laptop-capacity measurements.

`AnalyticsPipeline(..., reuse_enrichment=False)` disables reuse for qualification. It does not change the pipeline's semantic identity. A clean rebuild with deterministic adapters must match the cached build's full projection and graph at the same generation and source revision.

Analyzer reuse adds no optional ML dependencies or model weights. It does not qualify any model or change pricing availability. [Continuous processing](continuous-processing.md) describes the separate conversation-level optimization and its limits.

## Verification

```powershell
python -m pytest tests/test_enrichment_staging.py tests/test_enrichment_reuse.py tests/test_enrichment_cache_storage.py tests/test_enrichment_cache_contract.py
```

These tests count actual analyzer invocations, compare cold and reused artifacts, reopen encrypted stores in a new process, and test account isolation, changed inputs, context dependencies, expiry, failure, corruption, size limits, and analysis admission. Run the [analytics regression baseline](qualification.md) and the architecture checks as well.

The schema is an additive migration of the disposable analytics store. No authoritative migration or public response schema changes. Older builds that cannot open the newer projection schema must rebuild the disposable store through the existing recovery path.
