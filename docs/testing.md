<!-- CODE-VERIFY: Verify local commands, toolchain versions, CI jobs, qualification commands, and workflow links against package scripts and GitHub Actions before editing. -->

# Test changes

Run the backend lane that covers the changed code. The same Python runner selects tests locally and in CI. Bare `python -m pytest` keeps its existing broad default selection.

## Common local checks

Install the dependencies described in [Contributing](../CONTRIBUTING.md). Build the frontend and extension before collecting the whole backend suite; test imports use the generated assets.

| Task | Command |
| --- | --- |
| Fast backend tests | `python tools/test_backend.py fast` |
| Changed test | `python -m pytest tests/test_reply_source_selection.py -vv` |
| All backend integration tests | `python tools/test_backend.py integration` |
| One integration shard | `python tools/test_backend.py integration --shard 2` |
| One exhaustive Windows shard | `python tools/test_backend.py windows-full-regression --shard 1` |
| Full serial Windows default suite | `python tools/test_backend.py windows-full-regression` |
| Inspect an exhaustive Windows shard | `python tools/test_backend.py list windows-full-regression --shard 1` |
| Inspect a lane | `python tools/test_backend.py list integration --shard 2` |
| Validate all classifications and selectors | `python tools/test_backend.py list all --validate` |
| One stateful profile | `python tools/test_backend.py stateful --profile analytics_convergence_fast` |
| Windows platform contract | `python tools/test_backend.py windows-platform` |
| Windows analytics contract | `python tools/test_backend.py windows-analytics` |
| One scale qualification file | `python tools/test_backend.py scale -- tests/test_question_identity_scale.py` |

Arguments after `--` pass through to pytest. For example, `python tools/test_backend.py integration --shard 2 -- -k publication -vv` runs a filtered subset. The runner prints the command and writes reports under `artifacts/ci-tests/`. Profile variables apply only to the child process. `fast` runs the fast tier; CI also runs the named stateful profiles as separate steps in its backend-fast job.

Windows contract execution requires Windows and the fixed SQLCipher runtime. A Linux collection that skips a Windows-native test does not reproduce that test. Required hosted and packaged-runtime evidence still comes from their respective workflows.

Other local checks:

```powershell
python -m pytest
lint-imports
npm run check:architecture --prefix frontend
npm run check:architecture --prefix extension
npm test --prefix frontend
npm test --prefix extension
npm run build --prefix frontend
npm run build --prefix extension
npm run audit --prefix extension
```

## Protected architecture-impact gate

The pull-request gate classifies changed paths from the architecture manifest. Mapped invariants require an `affected` or `not affected` disposition. Affected invariants, enforced rules, authority relationships, and exceptions also require a rationale, named boundary, and safety evidence.

```powershell
python tools/check_boundary_declaration.py --base-ref origin/main --head-ref HEAD --pr-body-file .github/pull_request_template.md
python tools/check_boundary_declaration.py --changed-file frontend/src/components/ConversationCard.tsx --report-only
```

The first command validates a local pull-request body and diff. The second only reports path classification. [The standalone architecture workflow](../.github/workflows/architecture-impact.yml) handles pull-request body edits without rerunning the full CI workflow.

## Stateful ingestion tests

The Brain suite compares `HistoryRepository` with an independent state model. It covers repository transitions D01-D09, A01-A14, N01-N12, and N14-N20. Transport transitions S01-S03 and parser transition N13 have separate tests.

```powershell
python -m pytest tests/state_models/test_brain_ingestion_model_independence.py
python -m pytest tests/hardening/falsifiers/test_falsifiers.py
python -m pytest tests/stateful/test_brain_ingestion.py
$env:HYPOTHESIS_PROFILE="tier_a_general"; python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral
$env:HYPOTHESIS_PROFILE="tier_a_deletion"; python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionDeletion
```

The CI profiles run 15 general and 10 deletion histories. Use `dev` for a smaller local run and `--hypothesis-seed=<seed>` to replay a failure.

## Persistent ingestion tests

These tests reopen `HistoryRepository` over the same encrypted SQLite file and verify replay, staging, deletion barriers, and recovery.

```powershell
python -m pytest --basetemp .test-tmp-tier-b tests/stateful/test_brain_persistent_ingestion.py
$env:HYPOTHESIS_PROFILE="tier_b_general"; python -m pytest --basetemp .test-tmp-tier-b-general --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestPersistentGeneral
$env:HYPOTHESIS_PROFILE="tier_b_deletion"; python -m pytest --basetemp .test-tmp-tier-b-deletion --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestPersistentDeletion
$env:HYPOTHESIS_PROFILE="windows_persistence_smoke"; python -m pytest --basetemp .test-tmp-tier-b-smoke --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestWindowsProductionPersistenceSmoke
python tools/qualify_persistent_ingestion.py --output docs/architecture/persistent-ingestion-local-evidence.json
```

Windows CI builds and probes the pinned SQLCipher wheel. The release workflow also probes the frozen executable. Checked-in local evidence verifies persistence semantics but is not packaged-runtime evidence.

## Deterministic analytics rebuilds

This suite rebuilds one canonical state twice under a frozen `ReproducibilityContext`. The oracle compares semantic output, provenance, identities, graph content, digests, and referential closure.

```powershell
$env:HYPOTHESIS_PROFILE="analytics_determinism_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_determinism.py::TestAnalyticsDeterminism
python -m pytest --override-ini=addopts= tests/stateful/test_analytics_determinism.py
```

The CI profile runs 30 generated canonical states. Incremental convergence and persistent storage have separate suites.

## Agent delivery tests

The Agent suite drives `DurableIngestOutbox` and encrypted IndexedDB through a persistent Node harness. It compares each transition with the Python delivery model.

```powershell
$env:HYPOTHESIS_PROFILE="agent_tier_a_general"; python -m pytest --override-ini=addopts= tests/stateful/test_agent_delivery.py::TestAgentDeliveryGeneral
$env:HYPOTHESIS_PROFILE="agent_tier_a_deletion"; python -m pytest --override-ini=addopts= tests/stateful/test_agent_delivery.py::TestAgentDeliveryDeletion
python tools/qualify_agent_delivery.py
```

The CI profiles run 5 × 10 general and 4 × 10 deletion histories. [Local evidence](architecture/agent-delivery-local-evidence.json) records executed transitions, versions, timings, and the failing falsifier probe.

## Analytics convergence tests

These tests commit synthetic frames through `HistoryRepository`, update the live analytics pipeline, and compare active output with a clean rebuild. The oracle checks complete semantic content, provenance, graph closure, publication freshness, and deletion closure.

```powershell
$env:HYPOTHESIS_PROFILE="analytics_convergence_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::TestAnalyticsConvergence --hypothesis-show-statistics
$env:HYPOTHESIS_PROFILE="analytics_deletion_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::TestAnalyticsDeletionConvergence --hypothesis-show-statistics
python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::test_analytics_convergence_falsifiers_reject_metric_provenance_identity_graph_and_deletion_faults
python tools/qualify_analytics_convergence.py --output docs/architecture/analytics-convergence-local-evidence.json
```

CI runs six general histories of fourteen deliveries and four deletion histories of eleven deliveries. Stress profiles provide broader local runs. [Local evidence](architecture/analytics-convergence-local-evidence.json) records executed operations, restarts, rebuilds, timings, versions, and expected failures.

## CI coverage

GitHub Actions uses Python 3.11 and Node.js 22. [Product CI](../.github/workflows/ci.yml) runs fast backend checks, four isolated integration shards, Windows platform and analytics contracts, browser acceptance, and frontend/extension checks. Existing stateful profiles remain required. Every Windows consumer verifies the shared SQLCipher artifact's source and workflow run.

The web job starts independently and installs only JavaScript dependencies. It retains frontend and extension tests, architecture checks, the production builds and their existing lint/typecheck, generated-theme checks, provisioning module tests, analytics conformance, build audits and snapshot qualification. Documentation validation, the architecture manifest/declaration and Python import contracts run once in the required backend-fast job. A backend setup failure therefore does not prevent the web job from reporting its own results.

Browser acceptance runs on two Windows runners, `browser-e2e-core` and `browser-e2e-catchup`, with at most two active matrix jobs and one Playwright worker each. Every runner builds its own qualification assets. The checked-in browser registry assigns whole files; the runner compares that registry with an independent, unfiltered Playwright collection before executing a lane. The stable `windows-browser-e2e` check requires both lanes and the reporting-safety job, validates complete execution and source/run/producer provenance, and only then assembles the existing Legal evidence bundle. It remains a dependency of Required CI. The backend and full Windows requirements are unchanged.

The reporting-safety job exercises the actual pinned Playwright reporter with synthetic failures, retries, skips and interruptions, the existing JavaScript diagnostic redaction tests, and `tools/e2e-capture/tests/test_session_diagnostics.py` explicitly. That Python file is outside the ordinary backend `testpaths`; it must not rely on incidental discovery. See the [browser harness guide](../tools/e2e-capture/README.md) for the matching local commands.

Provisioning restart/resume controller checks have a different owner: `tests/test_provisioning_resume_browser_module.py` invokes `node --test app/provisioning/provisioning-resume.test.mjs` during backend tests. That Node module does not import browser dependencies. Browser finalization checks remain in the separate visual suite. Moving checks between these layers requires preserving their explicit invocation and dependency setup.

Main pushes and manual runs also require analytics scale qualification on Windows: long-idle lifecycle checks, the 100,000-record identity case, large graph reads, and SQLite crash/lease boundaries. Packaged-runtime and TPM qualification keep their separate artifact and hardware prerequisites. Product CI has no duplicate nightly schedule; other scheduled qualification workflows retain their own triggers.

The Required CI gate validates job results and executed coverage. The older build-and-test and windows-tests check names forward its result during branch-rule migration. Architecture impact remains independently required. Pull requests require the complete Linux default suite, focused Windows platform and analytics contracts, existing stateful profiles, and browser acceptance. Exhaustive Windows execution is required on every main push and manual run.

The exhaustive Windows regression runs in two isolated jobs, `windows-full-regression-1` and `windows-full-regression-2`, with at most two active runners. Files stay intact and tests run serially within each runner. The stable `windows-full-regression` aggregate always runs: it accepts a skipped producer only on a pull request and requires successful producers on main and manual runs. Required CI validates each producer's newest actual execution and complete evidence when exhaustive execution is required. The checked-in [Windows shard manifest](../ci/windows-full-test-shards.json) partitions the original marker expression, independently of analytics tiers and focused Windows contracts. Missing files, overlapping ownership, incomplete execution, mismatched source/run/attempt evidence and uncorroborated skips fail the gate.

Every pull request still collects the independent raw Windows inventory and proves each default case executes in Linux or its required Windows contract. A Linux skip cannot replace required Windows execution. The gate's closed cutover baseline permits only fourteen reviewed test identities with their exact execution platform and skip reason. Windows exceptions stay in their reviewed focused contract lane, and the four Linux process-filesystem cases must execute on Linux. Windows production boot requires actual execution of every selected case. New identities, changed reasons and skipped explicit profiles fail closed. Exhaustive main/manual runs retain same-run skip corroboration.

## Add or classify a backend test

Tests inherit their module's `pytestmark = pytest.mark.ci_tier("fast")` declaration. Use `integration` for database, pipeline, projection and graph lifecycle behavior. A test-level declaration overrides a module default; conflicting declarations at one scope are errors. Keep large-data correctness cases required. `scale` does not authorize removing a previously required test.

New tests in an existing integration file inherit that file's shard. For a new integration file, declare its tier and run `python tools/test_backend.py update-manifest`, then inspect the diff. Existing shard assignments stay unchanged. Add `windows_compat` when native behavior requires Windows execution and give it a reviewed Windows contract selector. `serial` describes tests that must not run concurrently on one runner.

New default tests in an existing file also inherit its exhaustive Windows shard. When adding a file, collect the raw inventory with `python tools/test_backend.py list windows-full-regression --output-dir artifacts/windows-inventory`, then run `python tools/test_backend.py update-windows-manifest --inventory artifacts/windows-inventory/report.json --dry-run`. Review the proposed addition and repeat without `--dry-run` to save it. This preserves existing assignments and records unmeasured files. The raw collection remains available even when a new file has no shard assignment or analytics classification.

Run `python tools/test_backend.py list all --validate` before submitting classification changes. Unclassified tests, missing assignments, stale selectors and overlaps produce actionable errors. Bare pytest remains available and keeps its previous marker exclusions.

## Diagnose or rerun CI

Open the failed job's summary first. It reports the failing node ID, phase, platform, profile and reproduction command. Detailed phase reports, JUnit and timing data are retained as artifacts for 14 days. Stateful failure output retains Hypothesis replay information when emitted.

If a job stops before its final summary, check the last `[ci-progress] start` line in the Windows job log and the artifact's `progress.jsonl`. The journal records collection, test phases and outcomes as they happen. Windows execution also prints a traceback after a test spends two minutes running; that diagnostic does not terminate the test or make a later pass a failure. Packaging smoke tests retain separate `packaging-smoke-*.jsonl` files with the last installer, listener and cleanup stage. Partial progress is diagnostic evidence; the gate still requires a complete final test report.

Subtests keep their own outcomes and sequence within the parent test. A passing parent cannot hide a failed or skipped child.

Use GitHub's **Re-run failed jobs** or **Re-run job** controls to retry the affected jobs. Successful jobs from earlier attempts of the same source and workflow run remain usable; a newer failure cannot be replaced by an older success. If required evidence has expired, start a fresh complete run. Tests are not automatically retried until green.

For browser failures, open the failing lane and its `browser-e2e-diagnostics-<lane>-<source>-<run>-<attempt>` artifact. This contains only validated, restricted metadata; malformed or missing reports produce a bounded diagnostic code. It does not upload arbitrary failed console output, error text, screenshots, traces or video. Complete `browser-e2e-inputs-...` artifacts are immutable and publish only after the producer evidence passes validation. The aggregate keeps their directories separate and selects evidence from each job's newest actual producer attempt; it cannot substitute an older pass for a newer failure. The stable `legal-activation-v2-<source>` artifact is replaced only after the complete aggregate succeeds.

Playwright's existing CI retry setting remains in force, and catch-up tests retain their existing zero-retry override. Browser reports distinguish a first-attempt pass from a retry pass; the backend paragraph above does not change this browser policy. For a focused local reproduction, use the ordinary Playwright command and the failed file or test title from the registry. A targeted run is diagnostic and cannot satisfy complete lane coverage.

For browser rollout comparisons, dispatch the existing [Product CI workflow](../.github/workflows/ci.yml) with both `browser_qualification` and `browser_serial_control` enabled. One run then executes the complete serial control alongside core/catch-up at the same source revision. Requesting serial control without qualification fails the safety job. The stable browser aggregate requires the requested control's newest successful evidence and exact inventory/outcome parity before producing the Legal bundle. Ordinary PR and main runs, and manual runs without that opt-in, skip this additional control while retaining both required browser lanes.

Qualification rejects any observed retry while preserving the underlying retry configuration, all journeys, deadlines and the catch-up negative-observation window. Record three distinct, complete, clean dispatches at the candidate SHA, each containing a serial/split pair, with exact identity/outcome parity, queue and bootstrap time, execution/assembly time, and runner minutes before making a rollout claim. Rerunning the same workflow run does not create a new comparison sample. A green serial control alone is not a paired comparison; these runs establish neither p95 latency nor visual-suite qualification.

After each hosted run finishes, save its metadata as `run.json` and its complete job-attempt history as `jobs.json` in one evidence directory. `python tools/browser_ci_metrics.py artifacts/browser-comparison/pair-1 artifacts/browser-comparison/pair-2 artifacts/browser-comparison/pair-3 --output artifacts/browser-comparison/timings.json` reports prerequisite/queue, bootstrap, execution, assembly and runner-minute measurements from those saved inputs. It reports medians and the slowest observation across samples without claiming qualification. Keep these timing observations alongside the independently validated coverage and outcome evidence.

Start each comparison with `gh workflow run ci.yml --ref <candidate-branch> -f browser_qualification=true -f browser_serial_control=true`. Repeat at the same candidate commit for three distinct runs. Download each run's `browser-e2e-inputs-*` artifacts with `gh run download <run-id> --pattern 'browser-e2e-inputs-*' --dir artifacts/browser-comparison/pair-1`, changing the directory for the other runs. Keep the original artifact directories. Then run `python tools/browser_ci_compare.py --run <run-1> artifacts/browser-comparison/pair-1 --run <run-2> artifacts/browser-comparison/pair-2 --run <run-3> artifacts/browser-comparison/pair-3 --output artifacts/browser-comparison/coverage.json`. This verifier reads fresh GitHub run/job history and the source-versioned registry and policy; it refuses failed jobs, repeated attempts, reused runs, mismatched source, retries, and missing execution outcomes. Saved summaries or a complete bundle alone cannot qualify a comparison.

Integration assignment is checked in. Download complete successful Linux timing artifacts, then run `python tools/test_backend.py update-manifest --rebalance --timings artifacts/ci-evidence --dry-run`. Review the assignments and repeat without `--dry-run` to save them. The importer combines setup, call and teardown times, takes the median across complete samples, and rejects targeted or stateful runs. A file-to-seconds JSON mapping is also accepted. Record the source run, sample count and report digest with each reviewed rebalance. A shard over ten minutes produces a maintenance warning; its twenty-minute timeout protects against runaway execution.

The exhaustive Windows manifest is balanced separately from complete Windows timings. Its initial measured files come from PR run 36970647763, with provenance and unmeasured additions recorded in the manifest. Use `update-windows-manifest --inventory <raw-report.json> --timings <complete-windows-report.json> --rebalance --dry-run` to review a rebalance; all current files need measurements. Rerun an individual Windows shard locally with the same command as CI, adding a node ID after `--` for a focused reproduction. Narrow local runs are useful diagnostics but cannot satisfy the complete hosted coverage gate.

## Roll out the parallel lanes

The telemetry-only [baseline run 36804164704](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/36804164704) passed at source `14adf8d7160929d141a5e34426bb19f9aafff6b1`, attempt 1, on Python 3.11.16. It retained the original selections. Its four jobs completed successfully in 48m 43s from the first job's start to the final job's completion.

| Baseline catch-all | Pytest wall time | Selected | Passed | Skipped |
| --- | --- | --- | --- | --- |
| Ubuntu | 23m 45s | 4,662 | 4,581 | 81 |
| Windows | 41m 37s | 4,665 | 4,651 | 14 |

The baseline's 131 integration files have complete Ubuntu measurements. The [shard manifest](../ci/backend-test-shards.json) records the source artifact, report digest and one sample per measured file. Those original assignments each contained about 316 seconds of recorded setup, call and teardown time, excluding bootstrap and collection.

The analytics expansion added 32 integration files with provisional assignments and preserved the original 131 assignments. Main integration adds catch-up, companion readiness, configuration restart, origin composition, native ACL and migration-history coverage. Existing assignments remain unchanged. Collect successful hosted timings for the complete current inventory before rebalancing; the older estimates do not describe the expanded suite. The baseline browser job took 12m 28s including its wheel dependency, so integration tests may no longer control the critical path after Windows shadow retirement.

The Windows PR cutover was owner-authorized on 2026-10-08 after three clean source-matched PR/manual pairs passed independent raw evidence review: [36979816565](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/36979816565) / [36979861591](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/36979861591), [37138860551](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/37138860551) / [37138944934](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/37138944934), and [37150620596](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/37150620596) / [37150647474](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/37150647474). Each pair had zero missing required node IDs, successful Windows contracts, complete execution, and unchanged permitted skip reasons. No existing default test may be demoted to optional scale execution.

Retain source-bound comparison receipts outside the product repository. The reviewed raw-audit receipt has SHA-256 `fa230a70cabe1ea85e2ae0904bc41eaacaa26e4f8f5f6f70127f5e102a2160db`. A failed or incomplete pair does not count toward the three required comparisons. This correctness evidence does not establish latency targets. Browser and visual CI changes need their own qualification.

Keep the existing required check names while deploying the new gate. Add Required CI to branch rules only after it has reported successfully on the current revision and open pull requests can produce it. Retire compatibility aliases in a later change; architecture-impact remains independently required. Historical release evidence continues to use the CI policy declared at its source revision.

After retiring the duplicate Windows PR run, measure both runner execution time and push-to-required-result time, including queue and bootstrap delays. Aim for the first actionable result within three to five minutes. The targets are backend p95 at most fifteen minutes and complete required CI p95 at most twenty minutes; publish the sample count with the percentiles. Use the first twenty representative completed PR runs as the initial review window. Three clean shadow comparisons establish correctness, not a credible p95. Also track total runner minutes so faster feedback does not conceal disproportionate cost.

[Visual capture](../.github/workflows/visual-capture.yml) runs separately for relevant Bridge changes. [Retention Phase-B evidence](../.github/workflows/retention-phase-b-evidence.yml) starts from a successful exact Product CI run instead of polling for one. The supplement remains an explicit workflow.

See [Product qualification](qualification.md) and [Windows acceptance](installation-and-acceptance.md) for release checks.
