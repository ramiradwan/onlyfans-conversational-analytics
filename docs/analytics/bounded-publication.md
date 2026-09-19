<!-- CODE-VERIFY: Check generation_reference.py, graph_verification.py, graph_row_encoding.py, projection_encoding.py, projection_verification.py, retention_store.py, pipeline.py, both SQLite stores, conversation_reuse.py, and qualify_continuous_analytics.py before editing behavior claims. -->

# Publish a staged generation without copying its graph

`build_candidate` returns a small stored reference for staged SQLite generations. `publish_candidate` verifies it and activates the complete generation without returning graph objects to the scheduler. The reference does not grant authority or replace validation of the stored data.

## Artifact access

`PipelineRun.artifact` explicitly reads the referenced artifact when the result is deferred. It checks the exact generation, source identity, digests, and expiry. It never substitutes the latest generation. Changed source data, expiry, discard, retirement, or a missing generation prevents that read.

The direct `project_account` and rebuild helpers materialize the staged artifact before publication, preserving their snapshot behavior even when sources change immediately afterward. Memory stores and projection catalogs without query metadata use inline artifacts.

The deferred result contains a reference and a reader callback, not cached message or graph objects. Requesting its artifact is account-sized work. Callers that need only publication success should not request the artifact.

## Verification and encoding

Graph verification reads ordered rows, validates their types and properties, and computes the canonical digest from their actual contents. An indexed join checks edge endpoints within the candidate. Account scope and persisted counts are checked independently. Matching two supplied digest strings is not sufficient. Stored content is verified during staging and again at the final activation gate, rather than once more between those gates. Final verification failure cancels the completed witness and retires the candidate.

When graph objects are not requested, the verifier checks stored columns directly using the same identity and closed-property rules as the public models. It normalizes timestamps and encodes the same canonical JSON without constructing temporary graph models. Non-integer or negative stored edge sequences are rejected. The verifier retains only the current row and counters. Full artifact reads materialize the validated graph explicitly. Database statement progress and row-level checks retain cancellation and deadline handling.

Validation-only staging and activation calls also check projection records individually. They calculate the digest from each normalized record and retain a separate metadata header, not message or conversation model arrays. Every record is checked at both gates. Full projection and artifact reads retain their ordinary complete models.

The streaming path handles the complete top-level field order produced by the canonical writer, including insignificant whitespace. Documents with another order or omitted default fields use the existing full-model compatibility path. The original JSON string remains in memory, so this is not a total memory cap. Cancellation is checked between streamed records.

Publication retention checks, timer setup and retired-generation cleanup use the earliest timestamp from the validated message records. They do not infer expiry from an unverified header date. Retired documents are processed one at a time. Full reads used for other retention operations keep their existing behavior.

Projection encoding excludes large arrays from the header serialization, then encodes one message or conversation record at a time. It preserves canonical field order, escaping, numbers, timestamps, and digest bytes. The resulting stored JSON string remains account-sized.

SQLite fragment staging validates one fragment against the complete artifact and inserts it before advancing. A validation failure rolls back the transaction. The built-in pipeline validates its privately owned graph record by record without cloning the complete graph before staging. The public staging and writer paths still copy and revalidate caller-owned records. Final verification rejects any change between input validation and storage.

## Remaining costs

The builder still assembles graph objects and complete projection inputs. It publishes a complete generation while [sharing unchanged physical graph content](shared-graph-storage.md). Streamed verification and a compact handoff do not make those operations incremental or establish a total memory cap.

Source-time expiry, canonical witnesses, ownership fencing, durable commits, property triggers, foreign keys, and backup checks are unchanged. No runtime dependency or model is added.

## Qualification

Run the focused tests and the [full analytics baseline](qualification.md):

```powershell
python -m pytest tests/test_projection_verification.py tests/test_graph_row_encoding.py tests/test_bounded_publication.py tests/test_generation_throughput.py tests/test_graph_digest_stream.py tests/test_conversation_fragment_storage.py
python tools/qualify_analytics_baseline.py --output C:\temp\bounded-publication-baseline
python tools/qualify_continuous_analytics.py --messages 10000 --query-samples 100 --output C:\temp\bounded-publication-workload
```

The workload reports candidate construction and publication before requesting an artifact. It records artifact materialization separately and compares that artifact with a clean rebuild. Cold and forced unchanged phases do not request unused artifacts. The publication peak and final diagnostic-process peak are distinct measurements.

Report private and working-set memory under an explicit guard for capacity tests. An incomplete workload does not establish supported capacity. Passing these tests does not qualify constrained laptops, the complete ingestion-to-interface journey, or production message classification.

Capacity runs can use `--verification-mode digests` to compare independently rebuilt projection, graph, and source digests with the verified stored reference. The selected comparison mode is recorded. Warm queries run before the independent rebuild, so that diagnostic computation does not consume the source-cache lifetime before query measurement.
