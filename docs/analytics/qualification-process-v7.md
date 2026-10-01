# Qualification process v7

This is a new measurement protocol, `a07-observable-scheduler.v7`, not a runtime
optimization. It must start a new source-bound campaign. Do not resume a v6
campaign, edit old receipts, hot-patch an active worker, or reuse a controller
whose candidate/manifest binding still names v6. Earlier failures remain failures.

## Verified-probe fail-fast

Visibility calls the same single-probe validator used by final verification only
AFTER `scheduled()` has completed synchronous product cleanup, drained the
backlog, rejected the stale reference, completed independent canonical/persisted
checks, and saved its phase record. A failure stops the remaining cases. The
worker writes `visibility-stop`, keeps completed probes, marks `complete=false`,
lists `unexecuted_cases`, and joins the scheduler. It never starts the restart
interpreter after an earlier failure. A failure in restart is propagated too.

An error during independent verification also stops work, but is recorded as an
error, not described as successfully verified. A failed resource close still
attempts scheduler shutdown; a failed shutdown prevents restart or acceptance.

Nothing can qualify a partial run: case-set and state-budget checks still require
the full sequence, and an explicit stop record always fails even if someone
changes `complete` to true. The supervisor may also report
`execution_states_incomplete` for an orderly early-failed exit. That is preserved,
not reclassified as success; the payload retains the specific failing probe.

Successful qualification still requires 100k messages, three fresh repetitions,
twelve probes, the original case order, 61 seconds idle, independent verification,
and all cleanup/currentness/shutdown checks. The reference limit remains exactly
10 seconds, with no rounding, and constrained profiles retain their original rule.
Neither 1800-second state limits nor the 9000-second visibility cap changes.

`--continue-after-visibility-failure` is an explicit diagnostic-only option for
`--run-source visibility`. It retains later cases for investigation, but the
verifier rejects it as qualification even if its timing samples happen to pass.
The option is bound in both worker input and payload. No automatic retry is added.

## Separate verification costs

Every `Workload.verify()` reports `verification_timings` with monotonic start/end,
total duration and four disjoint components: `resolve_generation`,
`canonical_reference`, `persisted_generation`, and `compare`. Existing full source
recomputation, persisted recomputation, fallback initialization, and all three
digest comparisons are retained. There is no expected-result cache or PASS cache.

The preliminary persisted check used when no active reference is present is
retained and charged to `resolve_generation`. Total time also includes small
reporting overhead, so component durations need not sum exactly to total.
Existing `independent_verification_seconds` remains the outer call measurement.
Do not sum that outer duration with its component durations.

## Progress without extra work inside the visibility gate

The collector's `progress.json` and its worker log now distinguish fixture/storage
initialization, cold/unchanged build and readiness, each verification component,
scheduler startup, restart reference capture, idle waiting and collector completion.
This advisory atomic status is not acceptance evidence and cannot reset deadlines.
Final phase and payload receipts remain durable. No new progress callback is
inserted into the scheduled commit-to-visible interval. Progress between phases
can still affect timing/cache age; that is why this is a new protocol.

## Validation and use

Use the host-admission launcher for any test or benchmark. Run the focused
protocol tests and the small executable collector tests before capacity work.
The small tests intentionally use 1000 messages and test-only limits and cannot
qualify performance. An injected tiny limit must yield a retained verified FAIL,
joined resources, an unexecuted restart, and a nonpassing state-budget verdict.

The current v6 campaign finished with an idle/dominant latency failure. Do not
silently restart it, remove its records, or relabel it after deploying this version.
This process improvement makes failed iterations cheaper; no successful-run speedup
has been measured. Optimize the verifier only after its new component timings
identify redundant work, without reducing independent checking.
