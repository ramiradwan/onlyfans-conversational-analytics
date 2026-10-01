<!-- CODE-VERIFY: Check light_first_update_benchmark.py, analytics_qualification_worker.py and acceptance-manifest.json before changing commands or behavior. -->
# Idle/dominant diagnostic prefix

Use the existing light runner with `--preparation repeat1-prefix --trace-mode none` for the unchanged-runtime baseline. Supply the usual signed `--source-root`, `--expected-sha`, fresh `--output`, and `--owner-lock`. Use native Windows Python and host admission. Start host observation before the worker, not after a probe.

The recipe `a07-idle-dominant-prefix.v1` invokes the v7 worker's `visibility()` directly. It preserves repeat 1's fresh construction, ordinary/dominant, forced unchanged rebuild, rebuilt/small, 61-second idle, and idle/dominant. It preserves the worker's independent verification, fail-fast decision, cleanup, and joined shutdown. It never invokes the restart collector.

The full execution schedule remains in the receipt, with `restarted` absent. The 1800-second state limits and 9000-second worker cap remain enforced. A shorter explicit diagnostic timeout is allowed; a longer one is rejected. `complete` describes diagnostic execution only. The prefix is never full qualification, even when every measured probe passes.

A verified earlier failure stops before idle and is reported separately. A 1000-message execution is a smoke test. Neither is evidence that the 100k idle miss reproduced. An unchanged-runtime non-reproduction ends this correction pass without a guessed runtime change.

`none` installs no trace patches. `coarse` retains the existing diagnostic wrappers and labels the prefix phases. Instrumented timings are attribution data, not qualification. Use matching instrumentation for any later baseline/candidate comparison and repeat without profiling before qualification.

The source receipt binds all tracked files, the manifest, runtime, runner, relevant helpers, and a separate hash of production `app/` files. Historical v6 receipts are not rebound to the v7 manifest. Keep them as separate evidence.

## Engineering references

Python distinguishes elapsed time from thread CPU time and warns that deterministic profiler timings are not benchmark measurements. SQLite documents automatic checkpoints during some commits and when the final connection closes. These justify separate timing boundaries, not a checkpoint-policy change or a conclusion about the cause of this application's miss.

- https://docs.python.org/3.11/library/time.html
- https://docs.python.org/3.11/library/profile.html
- https://www.sqlite.org/wal.html
- https://pyperf.readthedocs.io/en/latest/system.html
