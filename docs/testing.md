<!-- CODE-VERIFY: Verify local commands, toolchain versions, CI jobs, qualification commands, and workflow links against package scripts and GitHub Actions before editing. -->

# Test changes

Run the checks that cover the code you changed. CI is the final reference for the full test matrix.

## Common local checks

From the repository root:

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

Install backend development dependencies from `requirements-dev.txt` and JavaScript dependencies with `npm ci` in the relevant package before running these commands.

## Stateful property-based ingestion tests

Task 5A provides model-based state machine tests and falsifier harnesses for Brain canonical ingestion assurance (`checkpoint-monotonicity` and `replay-idempotency`):

The suite covers the 42 HistoryRepository-level catalogue entries (D01-D09, A01-A14, N01-N12, and N14-N20). Transport admission S01-S03 and parser validation N13 retain their separate seams. The two generated profiles run explicitly in the required Linux job and are excluded from ordinary backend collection to avoid a second dev-profile run.

```powershell
# Run independent model AST checks and subprocess isolation
python -m pytest tests/state_models/test_brain_ingestion_model_independence.py

# Run five permanent falsifiers proving oracle detection
python -m pytest tests/hardening/falsifiers/test_falsifiers.py

# Run deterministic catalogue, liveness, and metamorphic checks (Tier A classes are marker-excluded here)
python -m pytest tests/stateful/test_brain_ingestion.py

# Run the always-on PR profiles explicitly (15x20 general; 10x20 deletion)
$env:HYPOTHESIS_PROFILE="tier_a_general"; python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral
$env:HYPOTHESIS_PROFILE="tier_a_deletion"; python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionDeletion

# The dev profile is 8x12; smoke is 3x8. Show statistics or replay a seed:
$env:HYPOTHESIS_PROFILE="dev"; python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral --hypothesis-show-statistics
python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral --hypothesis-seed=<seed>
```

The research gate proposed 90x40 general and 60x30 deletion as starting calibration inputs. A dated local Task 5A run on 2026-09-09 was interrupted after 568.15 seconds before the 90x40 general test completed. In accordance with the plan's CI-budget rule, the repository profiles reduce example counts before removing transition families or oracle assertions. The always-on mix is 15 general histories and 10 deletion histories, so deletion-focused histories remain 40% of the generated PR total. The two exact profiles completed together in 49.03 seconds in the same local environment after consolidating observation reads. PR-runner timing and any further adjustment remain Task 5C/Task 9 evidence; local timing does not qualify the p95 target.

## Task 5C file-backed restart qualification

Task 5C drives the same `HistoryRepository` methods through fresh repository objects over one encrypted canonical SQLite file. It records accepted state, duplicate and gap recovery, staged snapshot material, deletion barriers, and JSON-safe trace replay across close/reopen cuts.

```powershell
python -m pytest --basetemp .test-tmp-tier-b tests/stateful/test_brain_persistent_ingestion.py
$env:HYPOTHESIS_PROFILE="tier_b_general"; python -m pytest --basetemp .test-tmp-tier-b-general --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestPersistentGeneral
$env:HYPOTHESIS_PROFILE="tier_b_deletion"; python -m pytest --basetemp .test-tmp-tier-b-deletion --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestPersistentDeletion
$env:HYPOTHESIS_PROFILE="windows_persistence_smoke"; python -m pytest --basetemp .test-tmp-tier-b-smoke --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestWindowsProductionPersistenceSmoke
python tools/qualify_tier_b_runtime.py --output docs/architecture/task5c-local-runtime-evidence.json
```

These profiles are marker-excluded from ordinary backend collection and are not CI-required production-equivalent evidence. The local `sqlcipher3==0.6.2` runtime reports SQLite 3.51.1 / SQLCipher 4.12.0, while the [SQLite WAL-reset advisory](https://sqlite.org/wal.html#walresetbug) requires a fixed SQLite runtime (3.51.3 or later). The local artifact is therefore only semantic file-backed evidence. It does not qualify a shipped Windows runtime, a runner p95 budget, or deletion closure for derived projections/graph state.

## Task 6A deterministic clean rebuilds

Task 6A rebuilds the same immutable canonical read model in two fresh in-memory
analytics runtimes under one frozen `ReproducibilityContext`. Its independent
oracle compares canonical witness, pipeline and analyzer provenance, message,
conversation, and participant identities, metrics, enrichments, graph nodes
and edges, graph digests, and graph referential closure. It excludes only the
contract's lifecycle fields: projection generation and store publication,
ownership, lease, and execution-time metadata.

```powershell
$env:HYPOTHESIS_PROFILE="task6a_determinism_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_determinism.py::TestAnalyticsDeterminism
python -m pytest --override-ini=addopts= tests/stateful/test_analytics_determinism.py
```

The selected CI profile is 30 generated canonical final states, with two fresh
clean builds per example. It is a local in-memory determinism check only: it
does not claim incremental/rebuild convergence, file-backed qualification, or
end-to-end derived deletion closure, which remain Task 6B and later evidence.

## CI coverage

GitHub Actions uses Python 3.11 and Node.js 22. In addition to the common checks, CI runs architecture boundary manifest validation, Python architecture boundary checks (`lint-imports`), Agent architecture boundary checks (`npm run check:architecture` in `extension`), Bridge architecture boundary checks (`npm run check:architecture` in `frontend`), contract-integrity tests, the provisioning-page module test, the 10,000-message Agent snapshot qualification, the backend suite on Windows, and capture end-to-end tests.

Bridge architecture boundary checks enforce acyclicity across protected protocol, store, and service namespaces (`rule-bridge-protected-acyclic`). This is phase-one structural protection: acyclicity prevents circular dependency chains from emerging but does not establish or prove architectural dependency direction.

See [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) for the current commands and job matrix.

Packaging, installed-artifact acceptance, and Beta qualification have separate checks. See [Product qualification](qualification.md) and [Windows acceptance](installation-and-acceptance.md).
