<!-- CODE-VERIFY: Timings and bindings come from full-attribution-20261001 result/operation receipts. Check conversation_graph_insertion.py, conversation_sql.py, shared_graph.py, conversation_integrity_store.py and conversation_id_frames.py before implementation. -->
# A07 graph-membership correction plan

## Decision

Plan one correction to graph-membership construction and independent stored verification on runtime `abf8aceb6ccd82363a7e9280aa5f7246d5382e7b`. Do not change runtime code in this attribution delivery. The measured work is repeated handling of large, mostly unchanged membership groups for a small tied insertion. Queueing, proof expiry, a full enrichment fallback and garbage collection are not the leading explanation in the measured focused update.

This identifies a correction area and implementation shape, not a proved passing candidate. Commit 3 remains unaccepted. The original repetition-1 idle/dominant result is 12.244234 seconds, including 6.932284 seconds before built, 3.883359 seconds to validated and 1.428591 seconds afterward. It remains FAIL.

## Measurement and limits

One native Windows 100k focused scheduled insertion ran with the original runtime application files, byte-checked against `abf8ace`. The signed diagnostic source was `aa8260448e09d3cb605cacaeee67cad4564f778d`. The existing light runner created a fresh 101-conversation fixture, including the previous ordinary/dominant message, independently verified cold output, and executed the same tied insertion through the scheduler.

It did not execute the forced unchanged rebuild, rebuilt/small mutation, 61-second idle or restart. It is instrumented source diagnostic evidence, not a qualification pass or attribution of every historical idle effect. One run was launched; none was retried. The original v7 manifest and runtime were unchanged.

| Interval | Instrumented focused insertion |
|---|---:|
| Commit to built | 6.634629 s |
| Built to validated | 3.744562 s |
| Validated to done | 0.821057 s |
| Total | 11.200248 s |

Both independent canonical/persisted comparisons passed, the stale reference was rejected, a current answer was observed, cleanup completed, and the scheduler closed with zero backlog and detached workers. No memory guard triggered. The complete diagnostic, including cold preparation and both independent verifications, took 1,090.743 seconds. This full-size calibration is not the intended per-edit loop.

The earlier metric correction is active: the matcher still checks 50,002 source records against 50,001 predecessor records but constructs zero historical metric inputs. Graph and enrichment predecessor proofs were present. Both incremental graph-delta verification and insertion validation succeeded. All 100 unchanged conversation units passed their unchanged-unit checks; no complete enrichment-unit or changed-segment-row fallback was observed inside the update.

## Attributed cost

| Disjoint operation | Elapsed | Thread CPU | What actually ran |
|---|---:|---:|---|
| Graph suffix construction, `replace_suffix` | 2.000621 s | 1.812500 s | 35 selected-content lookups for 37,439 identities; complete predecessor group handling and new unit encoding. |
| Changed-segment stored verification | 0.834585 s | 0.843750 s | 20 changed segments containing 46,875 records for only 19 changed keys. The same current chunk is traversed twice. |
| Conversation-integrity stored verification | 1.582736 s | 1.546875 s | Current unit membership verification, 20 selected-content lookups for 23,284 identities, and unchanged-unit checks. |
| Total selected graph interval | 4.417942 s | Do not infer exact wait from quantized CPU | These three spans do not overlap. |

Selected-content lookup costs are already included above: 0.809642 seconds in suffix construction and 0.524922 seconds in conversation-integrity validation. Do not add them again. The suffix span has 1.190979 seconds outside those lookups, and the integrity span has 0.792627 seconds outside its instrumented children. Group decoding, hashing and encoding inside those remainders were not individually timed; do not assign all of either remainder to one loop.

Other disjoint or subordinate costs remain: canonical identity scanning took 0.607637 seconds; graph writing took 1.017925 seconds; stored enrichment verification took 0.978021 seconds; generation-link checks took 0.320944 seconds. Graph writing reused 492 of 512 segments and 300 of 320 affected membership pages. Only 2,958 membership rows were written. This is not a full-graph rebuild.

Build execution used 9.562500 CPU seconds over 10.300104 elapsed seconds. Its executor queue wait was 0.000125 seconds; publication queue wait was 0.000071 seconds. Account-lock acquisition was below 0.00001 seconds. All 36 traced commits took 0.532581 seconds in total, with just 0.003921 seconds in the validation interval. Connection closes totaled 0.038493 seconds. Garbage collection was 0.014285 seconds in this fixture. These calls are nested in the stage totals, not additional time.

The stage partition leaves 0.096528 seconds before built and 0.175292 seconds after validated outside the measured build-worker spans. The latter includes observation/event-loop time. The entire validation interval is assigned to named spans at the selected granularity, but their internal self-time is not a complete microprofile.

Five-second external host samples around the update showed no counter errors, roughly 9.5–11.9% aggregate CPU, zero instantaneous physical-disk queue and average queue below 0.01. Those samples do not exclude brief contention. Cooperative host admission is not universal process-coverage or packaged-profile isolation evidence.

## Proposed correction: bound the graph-membership transition

### Construction: reuse proved immutable groups, not generic point lookups

Extend the existing append-side proof-bound group/chunk reader to eligible insertion construction. The reader must remain tied to the active predecessor, exact unit header, segment proof, store/schema stamp, account, pipeline configuration and source-retention window. Do not expose a caller-supplied boolean as authority.

Use `conversation_id_frames.trusted_groups` only under those existing verified bindings, retaining the complete parsing fallback. Derive genuinely changed records from the exact old/new suffix. Avoid reconstructing groups whose resulting records are unchanged, after establishing that the admitted source/proof permits that reuse. Read touched versions from hash-checked predecessor chunks through the existing loader-local reader instead of repeated `selected_content_ids` queries. Do not extend the reader beyond its transaction/lifecycle scope.

Preserve all insertion-specific checks: exact old-suffix content, no node deletion, at most one permitted message PRECEDES edge deletion, no unrelated replacement, correct ordinals, and exact final membership/root digests. Append and insertion do not have identical mutation permissions; sharing the reader must not erase that distinction.

### Stored validation: one independent stream, bounded sharing

Keep the validator independent of construction. Read actual candidate membership frames and the independently proved predecessor. For a byte-identical membership group, reuse only the established ID-shape/order result under the exact binding; still compute the candidate's complete unit/group digests from its actual bytes. Fully validate changed groups and cross-group boundaries. Identical membership IDs do not prove unchanged record content: content-version checks remain mandatory wherever a segment or referenced payload changed.

Fuse the two current-chunk traversals in `_verified_changed_segment_chunks` into one ordered merge with the actual predecessor and candidate membership rows. Preserve old and new digests/counts, category/identity checks, exact changed/removed sets, actual changed-payload validation, and endpoint coverage. Both iterators must be exhausted before a proof is returned. Retain cancellation and the existing transaction `total_changes` check.

Let `verify_generation_integrity` consume only the membership-to-content mappings it actually needs from that fresh validation, within the same transaction. Prepare the requested identities from actual candidate/predecessor units, not a constructor-supplied expected result. Keep storage bounded by the existing 32,768-row and 32 MiB limits, with streaming or per-group release. Do not retain all 46,875 changed-segment records: that would exceed the existing row cap. Missing coverage, exceeded limits, stale binding, legacy encoding or an incomplete stream must use the existing independently checked SQL/full-validation path or fail closed. No persistent cache, schema migration, new index or new proof lifetime is proposed.

### Budget and stopping decision

The 4.417942-second graph interval is the measured opportunity, not an achievable saving. Removing the historical 2.244234-second overage entirely from it would require about 50.8% less time, leaving at most 2.173708 seconds in a matched diagnostic. The two largest spans alone would require about 62.6% reduction. A small reader-only win or another frame-decoding saving is therefore not sufficient evidence of likely gate closure.

Use the existing failed campaign as the acceptance baseline, not a stopwatch comparison to this different instrumented fixture. Require a measured reduction in the selected full-update graph interval of at least 2.244234 seconds before claiming the planned budget is met; aim below that residual budget for margin. This is a sizing/decision criterion, not a guarantee that the exact idle prefix will pass. If preserving correctness cannot produce sufficient savings, stop with the measured residual and an explicit decision; do not automatically add a cache or another optimization.

## Fast implementation loop and acceptance

1. Extend `focused-component` with a graph-membership case that retains the 50,001-message predecessor's actual membership/chunk shape and tied insertion. Compare baseline/candidate from reset identical input. Avoid fresh full-account construction for every edit. Keep the independent clean result computation for the exercised graph and preserve candidate persisted-data validation; do not fabricate process-local publication proofs. Use fixture-only proof construction only in this declared component scope.
2. Assert the predicted work change: no generic selected-content queries inside the admitted construction path; current changed chunks visited once by stored verification; no extra selected-content lookup for a membership already covered by the transaction-local verified selection. Assert bounded retention, exact fallback and unchanged output, rather than accepting a lower elapsed total alone. Retain matching instrumentation and all predetermined samples. Three repeated paired component samples are diagnostic evidence, not qualification.
3. Run literal/corruption and independent-equivalence tests for additions, a tied insertion with boundary-edge removal, generic late insertion, edit, deletion, same IDs with changed content, false summaries, swapped accounts/generations, missing or altered chunk/trigger/proof metadata, expiry, restart, schema changes, cancellation and truncated iterators. Extend the existing incremental-conversation-integrity, graph-receipt, shared-membership-page and tied-insertion safety suites. Test both optimized and forced-fallback paths and memory-cap boundaries.
4. Run one matched full-size focused scheduled diagnostic to confirm that the identified graph costs fall without moving them into unmeasured preparation, validation or cleanup. Then repeat the original exact prefix without profiling, with verification in its original positions. Do not compare the component stopwatch directly to the ten-second visibility gate.
5. Only after the exact prefix passes, run the prescribed guarded visibility campaign. All twelve original probes across three fresh repetitions remain required for success. Any verified failure triggers the existing fail-fast stop and an explicit decision. Do not weaken the fixture, idle interval, correctness, cleanup or qualification requirements.

## Evidence binding

Attribution result: `C:\ofca-a07-full-attribution-20261001\focused-insert-100k\result.json`, SHA-256 `919b579d4272fe61abdd18f9b7dfb9e6cfd36ee8d5186d25b3612721e7feb6a2`.

Pre-oracle operation trace: same directory, `operation-attribution.json`, SHA-256 `1998808507c35b84820999dd413a6201a92570cb048653b409ce4e6a991c918e`. The final result, not that intermediate trace alone, establishes independent equality and joined shutdown.

Original failed qualification: `C:\ofca-a07-visibility-abf8ace-v7-20261001-r2\attempts\4f8a2e4b284441929e901c73d3cd5ac8\result.json`, SHA-256 `d3c1f002da1a6b0999f0311a69c90249c1831f472bd968fd2fea4893a2d7b48d`.
