# One bounded idle/dominant correction pass

## Recorded failure and control

Production revision: `6344dd27be1dc40f39cb73946ab8c3c11b99b11c`.
Controlled campaign: `C:\ofca-a07-controlled-6344dd2-20261001-r1`.
Repeat 1 receipt: `attempts/0f71a93592924566b89b73e0f760d56c/result.json`.
SHA-256: `1bc2bde79836e1e0df69966789543a7eae260a901a0fa47adea1f3e715925da2`.
The campaign completed repeat 1 and stopped before repeat 2; it remains FAIL.

| Interval | Ordinary/dominant | Idle/dominant | Additional elapsed time |
|---|---:|---:|---:|
| Commit to built | 4.560521 s | 6.925817 s | 2.365296 s |
| Built to validated | 2.496984 s | 4.106879 s | 1.609895 s |
| Validated to done | 0.776008 s | 1.238956 s | 0.462948 s |
| Total | 7.833512 s | 12.271652 s | 4.438140 s |

Idle cleanup completed at 12.178311 seconds; the last question took 0.086137
seconds. Post-activation cleanup took 0.535840 seconds. Eliminating that whole
cleanup tail would still not pass: validation alone ended at 11.032696 seconds.
All correctness checks passed. Restarted/small then passed at 5.760405 seconds.
Repeat 0's four probes passed; restarted/dominant was 8.664665 seconds.

The host guard reported CLEAN_SINCE_ATTACHMENT and no competing supported workload.
It covered the failed idle probe, not the whole campaign. External five-second
samples near the gate had no sustained high disk queue or CPU pressure; that is
not proof of no brief contention. Do not dismiss this failure as another agent's
backend test without evidence.

These two dominant probes occur at different points and source revisions. This
is an attribution comparison, not an experiment proving idle time alone caused it.

## Selected area, not a speculative patch

Target additional build/validation work after idle. The first investigation is
loss of safely reusable predecessor/identity proof state, repeated content reads,
or fallback full verification around expiry and maintenance. These are hypotheses.
Do not select larger caches, new indexes, a longer proof lifetime, or a different
checkpoint policy from the phase totals alone.

Need at least 2.272 seconds improvement to reach the unchanged 10-second gate.
Restoring ordinary/dominant build-plus-validation time while keeping the measured
idle tail would project to about 8.296 seconds. That is a sizing calculation, not
a measured performance claim. The existing eight-second engineering objective is
not a new gate. Preserve the already-passing restarted/dominant and ordinary paths.

## Measurement recipe

1. Extend the EXISTING light diagnostic with the repeat-1 qualification prefix:
   fresh 100k cold build and verification; ordinary/dominant update and verification;
   forced unchanged rebuild and verification; rebuilt/small update and verification;
   original 61-second idle; idle/dominant update and verification; normal shutdown.
   Stop there. No restart, question matrix, mutation matrix, extra warmup, manual
   checkpoint, or reopening of a prepared seed. Keep original message identifiers,
   timestamps, revision order, verification placement, maintenance and cleanup.
2. Bind this new v7 recipe, unchanged app-source tree, manifest/runtime/runner hashes,
   and expected canonical/graph/projection outputs. Historical v6 results are not
   relabelled v7 or silently mixed with it. First reproduce the miss under the new
   process protocol on unchanged production code. A passing non-reproduction cannot
   prove an implementation correction.
3. Collect bounded call/count attribution only for the ordinary and idle updates:
   source-identity scans and preparation; reuse hit/miss/fallback reason and age;
   changed conversation/page/graph-unit reads; build/stage; persisted validation;
   relevant commit/close durations; scheduler queue vs execution and maintenance
   overlap. Preserve elapsed and thread CPU separately. Do not invoke verification
   or checkpoint queries merely to observe state. Record host I/O alongside it.
   Use the existing diagnostic wrappers; do not add another independent harness.
4. If the same safe data is recomputed, implement one narrow reuse/lifecycle fix
   bounded to the verified account, source token, content/schema, generation and
   retention state. If a demonstrated maintenance overlap causes waiting, fix that
   ownership/scheduling path instead. If storage waits dominate, investigate those
   measured waits rather than treating proof expiry as established.

Only the smallest branch supported by the trace is implemented. If this prefix
cannot reproduce the miss, retain that outcome and return an explicit replanning
choice, rather than applying a guessed fix or looping indefinitely.

## Acceptance for the one correction

Keep the same diagnostic instrumentation in baseline and candidate runs. Demonstrate
improvement in the identified interval, not only total variation. Repeat the failed
idle prefix without profiling, including independent equality, stale rejection,
synchronous cleanup, zero backlog and joined workers. Run focused corruption,
expiry, source mutation, schema/token/account mismatch and shutdown regression tests
for whichever proof or scheduling path is changed. Never lengthen the ten-second
limit, the 10000-record query budget or proof lifetime as a shortcut.

Then run ONE source-bound visibility campaign with all three fresh repetitions and
all twelve probes; every required probe must pass. It uses shared host admission
and guard coverage from campaign start. No automatic retries. If a gate fails,
the new verified-probe fail-fast stops that repetition and the campaign stops
launching later jobs. Return the exact failed gate and an explicit next decision.

No production correction or new 100k measurement is part of delivering this plan.
The current runtime subject and the completed campaign remain unchanged.
