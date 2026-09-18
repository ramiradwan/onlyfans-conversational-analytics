<!-- CODE-VERIFY: Verify local commands, toolchain versions, CI jobs, qualification commands, and workflow links against package scripts and GitHub Actions before editing. -->

# Test changes

Run checks that cover the changed code. CI defines the full matrix.

## Common local checks

Install Python dependencies from `requirements-dev.txt`. Run `npm ci` in each changed JavaScript package.

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

GitHub Actions uses Python 3.11 and Node.js 22. [Product CI](../.github/workflows/ci.yml) keeps the four release-qualified jobs: Linux build and tests, the Windows SQLCipher producer, Windows backend tests, and Windows browser acceptance. Both Windows consumers verify that the SQLCipher artifact belongs to the exact source commit and workflow run before using it.

[Visual capture](../.github/workflows/visual-capture.yml) runs separately for relevant Bridge changes. [Retention Phase-B evidence](../.github/workflows/retention-phase-b-evidence.yml) starts from a successful exact Product CI run instead of polling for one. The supplement remains an explicit workflow.

See [Product qualification](qualification.md) and [Windows acceptance](installation-and-acceptance.md) for release checks.
