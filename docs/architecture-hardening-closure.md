# Architecture hardening closure baseline

This baseline is computed by `tests/test_architecture_baseline.py` from
`docs/architecture-boundaries.json`, executable controls, the import-contract
file, tracked repository paths, and generated local evidence. It covers the
repository-local portion of Task 9 at this revision.

The authoritative baseline is
`docs/architecture-boundaries.json`. The validator checks its schema and every
tracked production path; the protected-impact gate loads the same manifest;
and the architecture-contract tests check the Markdown rendering against it.
Unknown production paths fail classification.

The Task 5 and Task 6 entries qualified at closure have a requirement or
scenario, executable evidence, and a permanent falsifier: atomic canonical
commit, snapshot integrity, deletion closure, durable Agent delivery, graph
identity, and provenance integrity. The manifest names the exact test targets.
The other semantic entries retain their separately declared assurance status;
this closure does not promote a claim merely because a related test exists.

The current Task 7 composition closure is enforced by the import contracts and
their invalid fixtures: persistence factory does not construct analytics,
transport does not construct `HistoryAnalyticsSource`, services do not discover
transport, bootstrap owns canonical analytics-source construction, and
`CanonicalRepositories` has no `ingestion` member. The Task 2 census remains
the source justification for the five Python import contracts. Agent
capture/control and the protected Agent and Bridge dependency graphs retain
their negative fixtures. The cycle controls are phase-one structural
protection; they do not claim to prove every allowed dependency direction.

The only architecture exception is the narrow current-design
`app/persistence/projection_activation.py -> app.analytics` composition seam,
tracked as `DESIGN-PROJECTION-ACTIVATION-IDENTITY`. There are no temporary
exceptions. The validator rejects missing, expired, untracked, duplicate, or
out-of-scope exception records. The PR gate requires a disposition for each
machine-matched invariant, rationale for every not-affected disposition, and
evidence whenever protected impact is confirmed.

The fixed SQLCipher supply and Windows CI lane dynamically qualify the intended
runtime. The checked-in Task 5C artifact is deliberately a local,
advisory-affected probe: it records SQLite/SQLCipher versions, WAL and
synchronous settings, connection and checkpoint assumptions, advisories, and
an explicit `not production-equivalent` claim. It does not establish a hosted
or release-equivalent result. Task 5B and Task 6B evidence likewise records
real local profile work, restart/replay or convergence measurements, runner
versions, and falsifier/shrink configuration without representing a local run
as a PR-runner percentile.

The mandatory CI contract remains: Linux Brain, Agent, Task 6, architecture,
extension artifact and snapshot lanes; Windows fixed-runtime and Tier B lanes;
Windows browser/legal evidence; and the release-binding and signing gates.
ADR 0012 remains proposed.

## Computed repository-local counts

| Measure | Count |
| --- | ---: |
| Modules | 25 |
| Semantic invariants | 22 |
| Qualified semantic invariants | 13 |
| Unique permanent semantic-oracle falsifiers | 8 |
| Rules | 13 |
| Enforced rules | 11 |
| Documented rules | 2 |
| Rule-scoped permanent architecture negative controls | 11 |
| Python prohibited dependency contracts | 5 |
| Temporary exceptions | 0 |
| Current-design exceptions | 1 |
| Protected-impact classifier mappings | 22 |
| Unclassified tracked production paths | 0 |

The negative-control count is rule-scoped: the Agent dependency test module
contains controls for two rules. Run the focused closure checks:

```powershell
python -m pytest --override-ini=addopts= --basetemp=.pytest_temp_task9 tests/hardening/falsifiers/test_falsifiers.py tests/test_architecture_baseline.py tests/test_architecture_contracts.py tests/test_architecture_boundaries.py tests/test_architecture_runtime_policy.py tests/test_architecture_admission.py tests/test_docs.py
npm test --prefix extension -- tests/architecture-boundaries.test.mjs
npm test --prefix frontend -- tests/architecture-boundaries.test.ts
python tools/check_docs.py
```

These commands execute the Python, Agent, and Bridge architecture
negative-control modules. The closure test parses this table and compares every
label and value with counts derived from the current manifest and source
controls, so documentation drift fails the repository check.

## External closure gates

The repository has no representative hosted PR-runner sample with baseline
sample size, p50, p90, p95, or measured Tasks 5/6 added critical-path p95.
The required `<= 5%` result is therefore pending. No hosted Windows/release
runtime artifact is checked in for the fixed SQLCipher wheel. These two items
are external evidence gates, not passing repository-local controls. They must
be resolved with generated CI or release artifacts before describing the full
hardening plan as closed.

No production traces were used or accessed. The trace-minimization obligations
are therefore not applicable, and the repository-derived requirements and
independent assurance models remain the correctness basis for this baseline.
