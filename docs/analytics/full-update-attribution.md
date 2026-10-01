<!-- CODE-VERIFY: Check the CLI, UpdateAttribution, run_update and test_full_update_attribution.py before editing. -->
# Full-update diagnostic attribution

Use the existing light runner with `--preparation focused-update --messages 100000 --focused-operation insert --trace-mode coarse --full-attribution` to attribute a full-size scheduled insertion on a fresh synthetic fixture. This is not the forced-rebuild/idle prefix or qualification. No runtime source, proof lifetime, cache, checkpoint, acceptance threshold or fixture mutation changes.

The tracer extends the existing insertion spans with source preparation, account-lock acquisition, construction, graph suffix and segment work, staging, stored enrichment/graph/integrity validation, activation and cleanup. Context managers time acquisition and release separately; their bodies remain in the caller's span. Generator work is accounted for in its consuming bulk operation, not by timing iterator creation.

Each span reports monotonic elapsed and current-thread CPU time, its parent, self time, and relevant observed collection sizes. Self time subtracts nested synchronous spans on the same thread only. Executor submission/start/end remain in the existing coarse trace. SQL groups record execute/executemany counts and timing with a statement hash, store basename and enclosing operation. They never retain SQL text, arguments or encryption keys. Fetch and iterator work stays in the enclosing bulk span. SQL group times overlap spans and must not be added to them.

Tracing is bounded to 32,768 spans and 512 statement groups. Overflow fails the diagnostic, rather than dropping evidence silently. No row-level callbacks or diagnostic verification queries are added. No file write is added inside the scheduled interval. `operation-attribution.json` is saved after the completed operation, before the existing independent verification; final `result.json` contains the complete trace and correctness/shutdown receipt.

All callbacks preserve results and exceptions. Context wrappers preserve exception suppression and finalization. Inherited connection methods are removed again during restoration. The opt-in requires coarse tracing and focused-update mode. Unprofiled diagnostics and the v7 qualification recipe are unchanged.

Use the source-bound work counts and disjoint intervals to select one correction. Missing work remains an explicit remainder. Elapsed minus CPU is not proof of disk waiting. A shortened focused fixture cannot establish a proof-expiry or idle-specific mechanism without a lifecycle trace.
