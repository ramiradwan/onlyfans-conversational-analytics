<!-- CODE-VERIFY: Check the focused modes in light_first_update_benchmark.py and analytics_insertion_diagnostic.py, and the corruption/protocol tests before editing commands or claims. -->
# Focused insertion diagnostic

Use the existing light runner for fast attribution. These modes do not replace the unchanged v7 prefix or qualification campaign.

## Component scope

`--preparation focused-component` compares append and tied insertion from the same immutable predecessor. `--messages 100000` creates the dominant conversation's 50,001 predecessor records, including `visibility-ordinary-dominant`. The inserted `visibility-idle-dominant` sorts before that preceding message at the same timestamp. The append control sorts after it.

The runner executes the real source-match, metric, packing and stored-unit validation functions. It stores/reloads actual candidate bytes through the production enrichment-unit SQL and migration in an in-memory SQLCipher database. Its two minimal parent tables are fixture scaffolding, not a production generation or proof of publication authority. The predecessor receives complete unit validation before measurement.

Every sample rolls its candidate back. Each sample independently recomputes enrichment and metrics from the full synthetic source, then compares headers and decoded persisted bytes. Expected results are not cached. Analyzer-cache entries are empty in this component fixture; graph work, authority/proof admission, worker queueing, idle maintenance and disk waits are excluded. Use scheduled scope to check the real dispatch and these surrounding operations.

Three pairs run by default, with pair order alternating. All results are retained. Append versus insertion explains operation cost; it is not a before/after correction comparison. Compare baseline and candidate on the same insertion fixture, source/runtime binding, instrumented mode and sample order.

## Scheduled scope

`--preparation focused-update --focused-operation insert --messages 10000` uses a fresh 101-conversation qualification-shaped fixture. The prior ordinary/dominant message is present before cold construction. It uses the real scheduler, source/projection proof admission, publication, stale-reference rejection, independent graph/content verification and synchronous shutdown. The append control uses `--focused-operation append`.

This shortened fixture omits the forced unchanged rebuild, rebuilt/small update and 61-second idle. It does not reproduce their proof age or connection/maintenance history. At 1,000 messages the dominant conversation is below append's 1,024-record admission threshold; use 10,000 for append/insertion path comparisons. Do not infer that an immediate insertion measurement proves or disproves an idle-specific cause.

## Commands

Run serially through shared host admission. Substitute the signed revision being measured; use a new output directory each time.

```powershell
$source = 'C:\ofca-a07-focused-insertion-20261001\source'
$sha = (git -C $source rev-parse HEAD)
C:\Python313\python.exe "$source\tools\qualification_host_guard.py" run --label focused-insertion -- C:\Python313\python.exe "$source\tools\light_first_update_benchmark.py" --source-root $source --expected-sha $sha --preparation focused-component --messages 100000 --focused-repeats 3 --trace-mode coarse --output C:\ofca-a07-focused-component-new --owner-lock C:\ofca-a07-u3-closure-20260927\owner.lock
```

For integration dispatch use `--preparation focused-update --focused-operation insert --messages 10000`. For uninstrumented timing use `--trace-mode none`; no attribution patches are installed by the scheduled mode. The component mode installs inert pass-through wrappers only; no spans or bulk work counters are captured. Root construction, storage, validation and independent-verification durations remain separate.

## Reading the receipt

`result.json` binds the source file hashes, signed source revision, runtime, manifest, runner and helper. It always marks these modes diagnostic-only and nonqualifying. Sample records include accepted branch, input/output digests, independent equality and timings. Error, trace overflow or incomplete shutdown prevents successful completion.

Bulk spans record monotonic elapsed time, current-thread CPU, parent IDs and observed collection sizes. No source text, SQL arguments, row-level timers or in-operation diagnostic file writes are recorded. Nested durations overlap: use root intervals or `self_seconds`, not their sum. `seconds - thread_cpu_seconds` is not proof of disk waiting. The scheduled mode also retains existing executor submission/start/end and transaction/close tracing. Proof presence and predicate return values are observed without extra reads; a false predicate does not supply a more specific expiry or mismatch reason.

Do not interpret a component speedup as a ten-second visibility pass. After a measured correction and focused safety checks, confirm the exact unprofiled prefix and then run the prescribed qualification campaign.

## Measurement references

Python's [clock documentation](https://docs.python.org/3.13/library/time.html) distinguishes elapsed from thread CPU time. Its [profiler documentation](https://docs.python.org/3.13/library/profile.html) warns that profiling is not benchmarking. The [pyperf analysis guide](https://pyperf.readthedocs.io/en/latest/analyze.html) explains retaining samples and checking variability. These inform this diagnostic's measurement shape; none establishes an application-specific cause.
