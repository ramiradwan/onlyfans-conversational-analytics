<!-- CODE-VERIFY: Check source_snapshot.py, canonical_source.py, conversation_reuse.py, conversation_sql.py, graph_projection.py, pipeline.py, scheduling.py, runtime.py, both projection stores, and sql/0006_conversation_fragments.sql before changing behavior claims. -->

# Process changed conversations

The production analytics pipeline recomputes changed conversations and reuses unchanged conversation outputs. It then validates and publishes a complete graph and metrics generation. Publication remains complete. The built-in SQLite path also [reuses unchanged stored graph content](shared-graph-storage.md).

## Source reads and change detection

The canonical gateway streams the exact account digest used by the publication witness. The same pass records a digest for each conversation. It does not assemble a full account message model. Changed conversations are loaded individually, and their contents must match the recorded digest before computation. A supplied database connection remains under its caller's transaction control; the gateway never commits or rolls it back.

The digest covers the conversation metadata, ordered messages, text, direction, timestamps, and source ordinals. Appends, edits, deletions, late arrivals, participant changes, and retention changes therefore cannot silently reuse incompatible output. Account revision alone is not a reuse key.

An unchanged conversation supplies its message enrichments, metrics, and local graph records. A changed conversation uses the [per-message analyzer cache](enrichment-reuse.md), then rebuilds its metrics and local graph. Undeclared analyzer inputs, custom projectors, and unsupported sources retain the full computation path.

Canonical digest calculation covers source content outside the selected period. [Source verification tokens](read-verification.md) allow bounded reuse of an unchanged identity; cache misses still scan content. Post-commit scheduling reads only the account revision; it does not load message bodies.

## Graph assembly and visibility

Shared participants, topics, and entities use their stable identities. Only records supplied by current conversations enter the assembled graph. Participant conversation timelines are reconstructed from the current metrics, so deleting a middle conversation reconnects its remaining neighbors without retaining the deleted conversation.

Account aggregates are recalculated from current conversation metrics. Node and edge conflicts fail validation. The full generation retains the existing source-identity, publication-witness, and atomic activation checks. No reader sees updated edges paired with old metrics.

## Optional fragment storage

`conversation_fragments` lives in the existing encrypted analytics database. A fragment stores opaque references, source/configuration digests, source-time bounds, analysis, metrics, graph records, and reusable analyzer records. It does not copy message text or native conversation identifiers.

A build uses one witnessed predecessor connection for fragment reads. Only an active generation with a completed canonical witness supplies fragments. Current inputs, configuration, metric definitions, and retention must match. Missing or invalid records require recomputation.

Fragments are inserted while staging, checked against that generation's full artifact, and immutable afterward. Retirement and deletion remove them. Restart can reuse a still-valid active generation; recovery that retires it performs a cold build.

A build retains at most 4,096 fragments and 64 MiB of serialized fragment data. Each fragment is limited to 8 MiB. Oversized conversations still compute but are not retained as fragments. Analyzer-cache limits apply independently. These limits do not bound total process memory; full artifacts, graph records, source input, and Python objects require additional memory.

## Recovery and expiry

The default runtime checks canonical accounts every 30 seconds after starting its scheduler. This recovers dropped notifications, failed startup attempts, and expired projections even without a question request or new ingestion. One account's failure does not prevent checking later accounts. Work uses the existing bounded executor, queue, coalescing, and analysis-authorization checks.

Expired source data is refused before publication. Remaining permitted messages can be rebuilt at the same canonical revision. Input and context timestamps retain their original 90-day limit. The periodic task stops when the scheduler closes or resets.

The interval is an approximate retry cadence, not a promise that a rebuild finishes within 30 seconds. Canonical checks and full generation work can take longer on large accounts. Failed authorization produces an unavailable or failed analysis state, not an unlicensed rebuild.

## Verification and measurement

Run the focused tests and the [analytics regression baseline](qualification.md):

```powershell
python -m pytest tests/test_continuous_analytics.py tests/test_continuous_recovery.py tests/test_conversation_fragment_storage.py
python tools/qualify_analytics_baseline.py --output C:\temp\continuous-baseline
python tools/qualify_continuous_analytics.py --messages 1000 --query-samples 100 --output C:\temp\continuous-workload
```

Each output directory must be new. The workload command uses isolated synthetic encrypted stores, with half the messages in one conversation and the remainder across 100 conversations. It records cold, forced unchanged, and one-message-update work; actual analyzer calls; conversation body reads; identity scans; publication time; process peak memory; and saved-question latency, including failures. It also compares the updated artifact with a full rebuild.

The forced unchanged phase measures rebuilding with reuse, not the ordinary unchanged no-op path. Query results retain unknown message-type coverage. A successful query timing does not qualify the no-reply classification or pricing accuracy.

Source scans, full graph serialization, validation, and generation writes remain workload costs. The [laptop workload and acceptance targets](qualification.md) remain separate gates. This command does not establish installer size, constrained-laptop performance, or production feature accuracy.

No model, runtime dependency, public protocol change, or additional database is required. Conversation fragments belong to the disposable analytics store. Source-verification tokens and the date index also have an additive canonical migration, described in [Read verification](read-verification.md).

## Source catalog reuse

The canonical gateway can reuse an account catalog for the existing 60-second source-identity lifetime. The current transaction-maintained source token and identity must still match. The cache holds at most eight accounts, 4,096 conversations per account and 512 KiB of digest-key bytes in total, excluding Python overhead. It retains no message text. Returned maps are separate copies. Reads do not extend expiry.

Mutation, missing tracking, schema change or restart prevents ordinary cache reuse. Identity-cache clearing or expiry also removes the normal cache entry. A same-process HMAC proof minted by a full scan can rebind that exact identity after cache expiry only when a later transaction reads the same source token. Caller-owned canonical connections still read their own snapshot. Canonical witness transactions retain independent source checks and accept no raw worker-supplied digest; they may consume the authenticated process proof after independently matching its token, otherwise they use the complete identity path.

## Reconciliation cost

A successful complete currentness check can be reused for 60 seconds while its generation, source identity, pipeline and storage-change stamp remain identical. Each reuse still checks the completed witness and source expiry. This bounded metadata cache avoids repeatedly reading an unchanged graph during adjacent scheduler polls. It is separate from activation receipts and never grants analysis authority.

A cold check, expired proof or changed storage stamp still requires a complete validated projection read. This is not a constant-time guarantee for cold reconciliation or changed conversations.

For capacity measurements that need cold build followed directly by a real update, pass `--skip-unchanged-rebuild`. The report records that omission explicitly. It does not qualify forced unchanged rebuilds or make incomplete oracle verification a passing run. Keep the default workload for diagnosing repeated full-generation rebuild cost.

On Windows, workload phases also record process I/O transfer and operation deltas when available. These cover the benchmark process, including temporary files and logs; they are not database-only disk-write measurements. Do not compare them directly with physical-disk counters from a different platform.
