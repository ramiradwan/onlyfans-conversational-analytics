# First-update reproducer

This extends the existing external `light_first_update_benchmark.py` diagnostic.
It is not a new qualification suite and makes no production changes.

## Frozen cold recipe: `a07-cold-first-update.v1`

Create a fresh synthetic `Workload` using normal storage initialization, 100,000
messages, 101 conversations, the checked-in evaluation clock, and production
unknown message kinds. Do not copy or reopen a prepared database.

Create the question resources and one-worker scheduler before preparation, as in
`analytics_qualification_worker.visibility`. Call the unchanged qualification
`direct(..., 'cold')` helper, including its ordinary currentness check, question,
and independent rebuild verification. Keep the same process, workload, stores,
and event loop alive. Start question resources and scheduler recovery. Immediately
call the unchanged `scheduled` helper for `visibility-ordinary-small`, including
its independent verification after timing. Close resources and join the scheduler.

This is only the cold-to-first-update prefix. It does not run unchanged rebuild,
idle, restart, questions, mutation matrices, or packaged qualification.

Cold mode rejects a seed argument and any inserted idle. It does not use
`verified_seed_storage_start`, manually prepare question caches, checkpoint a
database, alter WAL settings, or change retention, validation, cleanup, or limits.

The 1,000-message option is a smoke test, never a 100k performance result.

## Evidence and interpretation

Each run binds the clean signed subject SHA, runner hash, helper hashes, runtime,
manifest, and (when supplied) the original failed operation. A baseline mismatch
in runtime, manifest, message count, case, or message-kind adapter stops the run.
Started and final receipts are separate. An interrupted run cannot become a pass.
The owner lock, process check, disk preflight, memory guard, and watchdog remain.

Timing starts at durable canonical commit and ends at the latest of a valid
current answer, required cleanup, and drained backlog. The recorded publication
boundaries partition that interval. The original ten-second gate is unchanged.

The trace records transaction entry/body/exit, connection closing, journal writes,
owned-executor and asyncio-executor queue waits, per-thread CPU time, and passive
checkpoints. WAL observations use file sizes only; they do not issue SQL. File
size is not a measurement of outstanding uncheckpointed frames. Checkpoint return
values describe only checkpoints that actually occurred. Nested trace durations
must not be added as if they were disjoint wall-clock intervals.

`LATENCY_MISS_REPRODUCED` means a complete 100k cold run exceeded ten seconds.
It does not establish the same mechanism, a statistical effect, or WAL causation.
`NOT_REPRODUCED` means the run does not establish that latency miss. A smoke result
or incomplete run cannot support causal comparison or qualification.

The causal sequence remains: unchanged `6344dd2` misses, `3d9be12` passes, then
`6344dd2` misses again, using the same frozen preparation, runner, and runtime.
Do not advance to a production optimization from a green ready-seed result.
Retain all failures and non-reproductions. Do not overwrite existing run outputs.

## Running the cold prefix

Use a separate clean signed subject checkout. Keep the diagnostic outside that
checkout. Use a new output directory and the same owner lock as qualification.
The cold mode always verifies after the probe; `--verify-after` is only needed
for the older ready-seed mode.

```powershell
& C:\Python313\python.exe -u C:\ofca-a07-final-visibility-20260929\light_first_update_benchmark.py `
  --source-root C:\ofca-a07-final-visibility-20260929\source `
  --expected-sha 6344dd27be1dc40f39cb73946ab8c3c11b99b11c `
  --preparation cold --messages 100000 --case small `
  --baseline-operation C:\ofca-a07-visibility-qualification-6344dd2-20260930\attempts\a46340fdfc41490685e48669df7f9b58\collector\events\00009-operation.json `
  --output C:\ofca-a07-final-visibility-20260929\cold-first-B-small-100k-v2-r1 `
  --owner-lock C:\ofca-a07-u3-closure-20260927\owner.lock `
  --timeout-seconds 1800
```

This records the first 100k invocation; choose a new output name to repeat it.
A process exit of zero means the diagnostic completed, not that latency passed
or the WAL hypothesis was established. Inspect `result.json` and its safety,
verification, source, baseline, summary, and reproduction fields.

Protocol tests, without a performance workload:

```sh
python3 -m unittest tests.test_light_first_update_benchmark -v
```
