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

These profiles remain marker-excluded from ordinary backend collection. The required Windows CI path builds the pinned fixed wheel, records a dynamic SQLCipher qualification report, and explicitly runs the general, deletion, and bounded smoke profiles. The release package workflow separately runs the frozen-executable probe. Dated local Task 5C evidence measured 65.283443 seconds for general, 48.628686 seconds for deletion, and 10.400190 seconds for smoke; hosted runner timing is recorded before any profile reduction. The local `sqlcipher3==0.6.2` runtime reports SQLite 3.51.1 / SQLCipher 4.12.0, while the [SQLite WAL-reset advisory](https://sqlite.org/wal.html#walresetbug) requires a fixed SQLite runtime (3.51.3 or later). `packaging/sqlcipher/` supplies SQLCipher 4.17.0 from checksum-pinned inputs, but production-equivalent evidence still requires a successful hosted CI run and frozen package run with their retained provenance. Until then, local file-backed results qualify only semantic persistence, not a shipped Windows runtime, a runner p95 budget, or deletion closure for derived projections/graph state.

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

## Task 5B Agent durable-delivery qualification

`tests/stateful/test_agent_delivery.py` keeps one Node JSON-lines process alive for each generated history. It drives the real `DurableIngestOutbox` and encrypted IndexedDB storage adapter over the repository FakeIndexedDb, then compares every transition with the independent Python delivery model. The explicit CI profiles are 5 × 10 general histories and 4 × 10 deletion histories; deletion therefore receives 44.4% of the bounded history budget. The calibration was reduced from the plan's starting count before removing any transition family.

```powershell
$env:HYPOTHESIS_PROFILE="agent_tier_a_general"; python -m pytest --override-ini=addopts= tests/stateful/test_agent_delivery.py::TestAgentDeliveryGeneral
$env:HYPOTHESIS_PROFILE="agent_tier_a_deletion"; python -m pytest --override-ini=addopts= tests/stateful/test_agent_delivery.py::TestAgentDeliveryDeletion
python tools/qualify_agent_tier_a.py
```

The bounded local profiles are 5×10 general and 4×10 deletion (44.4% of the configured Agent histories). [`task5b-agent-local-evidence.json`](architecture/task5b-agent-local-evidence.json) records the histories and Driver transitions actually executed, Node process count, reconnect/restart/snapshot/deletion operation counts, runner versions, and a deliberately failing Hypothesis falsifier probe. It records no PR-runner percentile; Hypothesis does not expose a separate shrinking-phase timer. Local timings are calibration evidence only and do not establish derived projection closure or Task 9 closure.

## Task 6B incremental convergence and derived deletion closure

Task 6B delivers every synthetic canonical frame through `HistoryRepository`,
runs the normal `AnalyticsPipeline.project_account` path after every delivery,
and compares its active artifact with a clean build in fresh in-memory
projection and graph stores. The independent oracle derives expected output
from the final canonical `AccountReadModel` and frozen context, then compares
the canonical witness; pipeline and analyzer provenance; message, conversation,
participant, topic, and entity identities; nested enrichments; metrics; graph
nodes, edges, sequences, properties, and semantic digest; and
active-publication freshness.
Only documented lifecycle values such as projection generation and its composite
digest are excluded.

The test factory's `memory` selection still creates temporary file-backed
SQLCipher canonical and repository-projection databases; it is the real canonical
`HistoryRepository` authority. Only the incremental and clean analytics
projection/graph stores are fresh in-process stores. This profile therefore
does not claim production-equivalent fixed-runtime persistence.

```powershell
$env:HYPOTHESIS_PROFILE="task6b_convergence_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::TestAnalyticsConvergence --hypothesis-show-statistics
$env:HYPOTHESIS_PROFILE="task6_deletion_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::TestAnalyticsDeletionConvergence --hypothesis-show-statistics
python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::test_task6b_falsifiers_reject_metric_provenance_identity_graph_and_deletion_faults
python tools/qualify_task6b_convergence.py --output docs/architecture/task6b-local-evidence.json
```

The checked-in fast CI mix is six general histories of fourteen deliveries and
four deletion histories of eleven deliveries. Every history contains its full
operation family, an alternative chat delivery order, an idempotent retry, and
a real, distinct `CanonicalSQLite` wrapper plus
`HistoryRepository`/`HistoryAnalyticsSource` reconstruction over the same
temporary canonical database path, key scope, and timeout. The deletion
histories perform that restart
after tombstones and before duplicate replay, stale reappearance, and a final
clean rebuild. Only the example count was reduced after local measurement.
The retained `task6b_convergence_stress` (30) and `task6_deletion_stress` (20)
profiles preserve the research-gate breadth for offline calibration. The local
measurement is recorded in
[`docs/architecture/task6b-local-evidence.json`](architecture/task6b-local-evidence.json).
That artifact is generated from test-owned runtime instrumentation and records
actual histories, canonical mutation deliveries, clean rebuilds, repository
restarts, alternative-order histories, deletion and duplicate deliveries,
wall-clock samples, runtime versions, and an expected failure/shrink
invocation. It is not PR-runner p95 closure,
production-equivalent persistence evidence, or Task 9 hardening closure.

The deletion profile checks an individual message tombstone, a conversation
cascade with temporal edges, last-participant removal, duplicate deletion,
and rejected semantic reappearance through enrichments, metrics, graph nodes,
edges, referential closure, and complete materialized active publication
content. Its permanent falsifiers cover metric, provenance, identity, graph,
deletion-closure, and stale-but-current-witness active-publication corruption.

## CI coverage

GitHub Actions uses Python 3.11 and Node.js 22. In addition to the common checks, CI runs architecture boundary manifest validation, Python architecture boundary checks (`lint-imports`), Agent architecture boundary checks (`npm run check:architecture` in `extension`), Bridge architecture boundary checks (`npm run check:architecture` in `frontend`), contract-integrity tests, the provisioning-page module test, the 10,000-message Agent snapshot qualification, the backend suite on Windows, and capture end-to-end tests.

Bridge architecture boundary checks enforce acyclicity across protected protocol, store, and service namespaces (`rule-bridge-protected-acyclic`). This is phase-one structural protection: acyclicity prevents circular dependency chains from emerging but does not establish or prove architectural dependency direction.

See [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) for the current commands and job matrix.

Packaging, installed-artifact acceptance, and Beta qualification have separate checks. See [Product qualification](qualification.md) and [Windows acceptance](installation-and-acceptance.md).
