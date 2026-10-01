<!-- CODE-VERIFY: Check PrefixAttribution, the light-runner opt-in and prefix tests. -->
# Exact-prefix update attribution

Use `--preparation repeat1-prefix --trace-mode coarse --full-attribution` for a new instrumented diagnostic. It reuses the original v7 worker, case order, mutations, 61-second idle, verification positions, fail-fast behavior, cleanup and deadlines. It cannot qualify latency.

Detailed spans are enabled only around the three scheduled updates. Sparse executor envelopes cover the lifecycle, including maintenance submitted or started before an update. The tracer records clocks, thread IDs, function names, work counts and exception types, not argument values or source content.

The original operation record is saved first. An additional snapshot is saved outside the timed interval and explicitly marks independent verification as pending. Only the final result can establish equality and joined shutdown. Snapshot writes may affect cache age; this is not the unprofiled recipe.

The total span and SQL-group budgets are three times the existing per-update limits. Executor calls are limited to 4,096. Overflow fails explicitly. Cold and oracle content work is not traced. No extra database queries, checkpoints or production proof refreshes are added.

Wrappers are disabled between updates and restored after the worker closes its scheduler. A window-close race preserves the original method call. Exceptions and cancellation restore the original methods. An unprofiled prefix installs none of these patches.

Validation includes a real 1,000-message prefix, all three independent checks, zero backlog and detached workers, bounded buffers, cancellation, restoration, and operation-before-oracle order. Full qualification still requires all twelve prescribed probes.
