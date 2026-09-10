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

## Protected architecture-impact PR gate

The pull-request gate reports a deterministic `architecture_impact` object from
the machine manifest. Risk-zone colour is context only. A mapped invariant needs
one explicit `affected` or `not affected` disposition; a confirmed affected
invariant, enforced rule, authority/trust relationship, or exception-ledger
change also needs meaningful rationale, a named boundary, and safety evidence.

For a local PR-style run, provide a checked-out base, head, and body file. The
command reads the event/body and Git diff locally and does not call GitHub:

```powershell
python tools/check_boundary_declaration.py --base-ref origin/main --head-ref HEAD --pr-body-file .github/pull_request_template.md
```

For a path-only classifier diagnostic, use explicit changed-file input and omit
the declaration check:

```powershell
python tools/check_boundary_declaration.py --changed-file frontend/src/components/ConversationCard.tsx --report-only
```

The required `build-and-test` pull-request job passes the GitHub event payload
and base/head SHAs to the same command. Push builds retain their existing checks;
the declaration gate is intentionally a pull-request-only review control.

## Stateful property-based ingestion tests

The Brain ingestion suite uses an independent model, generated state-machine histories, and permanent falsifiers to verify checkpoint monotonicity and replay idempotency.

The suite covers the 42 HistoryRepository-level catalogue entries (D01-D09, A01-A14, N01-N12, and N14-N20). Transport admission S01-S03 and parser validation N13 retain their separate seams. The two generated profiles run explicitly in the required Linux job and are excluded from ordinary backend collection to avoid a second dev-profile run.

```powershell
# Run independent model AST checks and subprocess isolation
python -m pytest tests/state_models/test_brain_ingestion_model_independence.py

# Run permanent falsifiers proving oracle detection
python -m pytest tests/hardening/falsifiers/test_falsifiers.py

# Run deterministic catalogue, liveness, and metamorphic checks
python -m pytest tests/stateful/test_brain_ingestion.py

# Run the CI profiles explicitly (15x20 general; 10x20 deletion)
$env:HYPOTHESIS_PROFILE="tier_a_general"; python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral
$env:HYPOTHESIS_PROFILE="tier_a_deletion"; python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionDeletion

# The dev profile is 8x12; smoke is 3x8. Show statistics or replay a seed:
$env:HYPOTHESIS_PROFILE="dev"; python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral --hypothesis-show-statistics
python -m pytest --override-ini=addopts= tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral --hypothesis-seed=<seed>
```

The CI mix is 15 general histories and 10 deletion histories, so deletion-focused histories receive 40% of the generated history budget. Profile tuning may reduce example counts, but it must retain every transition family and oracle assertion. Local timing is calibration data only; hosted-runner measurements determine whether the suite meets its p95 budget.

## File-backed restart qualification

The persistent-ingestion suite drives the same `HistoryRepository` methods through fresh repository objects over one encrypted canonical SQLite file. It records accepted state, duplicate and gap recovery, staged snapshot material, deletion barriers, and JSON-safe trace replay across close/reopen cuts.

```powershell
python -m pytest --basetemp .test-tmp-tier-b tests/stateful/test_brain_persistent_ingestion.py
$env:HYPOTHESIS_PROFILE="tier_b_general"; python -m pytest --basetemp .test-tmp-tier-b-general --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestPersistentGeneral
$env:HYPOTHESIS_PROFILE="tier_b_deletion"; python -m pytest --basetemp .test-tmp-tier-b-deletion --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestPersistentDeletion
$env:HYPOTHESIS_PROFILE="windows_persistence_smoke"; python -m pytest --basetemp .test-tmp-tier-b-smoke --override-ini=addopts= tests/stateful/test_brain_persistent_ingestion.py::TestWindowsProductionPersistenceSmoke
python tools/qualify_persistent_ingestion.py --output docs/architecture/persistent-ingestion-local-evidence.json
```

These profiles are marker-excluded from ordinary backend collection. Windows CI builds the pinned fixed wheel, records a dynamic SQLCipher qualification report, and runs the general, deletion, and bounded smoke profiles. The release workflow separately probes the frozen executable. The checked-in local evidence uses an advisory-affected runtime and therefore qualifies semantic persistence only. Production-equivalent evidence requires successful hosted CI and release-package runs with retained fixed-runtime provenance.

## Deterministic clean rebuilds

The determinism suite rebuilds the same immutable canonical read model in two fresh in-memory
analytics runtimes under one frozen `ReproducibilityContext`. Its independent
oracle compares canonical witness, pipeline and analyzer provenance, message,
conversation, and participant identities, metrics, enrichments, graph nodes
and edges, graph digests, and graph referential closure. It excludes only the
contract's lifecycle fields: projection generation and store publication,
ownership, lease, and execution-time metadata.

```powershell
$env:HYPOTHESIS_PROFILE="analytics_determinism_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_determinism.py::TestAnalyticsDeterminism
python -m pytest --override-ini=addopts= tests/stateful/test_analytics_determinism.py
```

The selected CI profile is 30 generated canonical final states, with two fresh
clean builds per example. It is a local in-memory determinism check only: it
does not claim incremental/rebuild convergence, file-backed qualification, or
end-to-end derived deletion closure; those properties have separate suites.

## Agent durable-delivery qualification

`tests/stateful/test_agent_delivery.py` keeps one Node JSON-lines process alive for each generated history. It drives the real `DurableIngestOutbox` and encrypted IndexedDB storage adapter over the repository FakeIndexedDb, then compares every transition with the independent Python delivery model. The explicit CI profiles are 5 × 10 general histories and 4 × 10 deletion histories; deletion therefore receives 44.4% of the bounded history budget.

```powershell
$env:HYPOTHESIS_PROFILE="agent_tier_a_general"; python -m pytest --override-ini=addopts= tests/stateful/test_agent_delivery.py::TestAgentDeliveryGeneral
$env:HYPOTHESIS_PROFILE="agent_tier_a_deletion"; python -m pytest --override-ini=addopts= tests/stateful/test_agent_delivery.py::TestAgentDeliveryDeletion
python tools/qualify_agent_delivery.py
```

The [Agent delivery evidence](architecture/agent-delivery-local-evidence.json) records the histories and driver transitions actually executed, Node process count, reconnect/restart/snapshot/deletion operation counts, runner versions, and a deliberately failing Hypothesis falsifier probe. It records no hosted-runner percentile; Hypothesis does not expose a separate shrinking-phase timer. Local timings are calibration evidence only and do not establish derived projection closure.

## Incremental convergence and derived deletion closure

The convergence suite delivers every synthetic canonical frame through `HistoryRepository`,
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
$env:HYPOTHESIS_PROFILE="analytics_convergence_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::TestAnalyticsConvergence --hypothesis-show-statistics
$env:HYPOTHESIS_PROFILE="analytics_deletion_fast"; python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::TestAnalyticsDeletionConvergence --hypothesis-show-statistics
python -m pytest --override-ini=addopts= tests/stateful/test_analytics_equivalence.py::test_analytics_convergence_falsifiers_reject_metric_provenance_identity_graph_and_deletion_faults
python tools/qualify_analytics_convergence.py --output docs/architecture/analytics-convergence-local-evidence.json
```

The checked-in fast CI mix is six general histories of fourteen deliveries and
four deletion histories of eleven deliveries. Every history contains its full
operation family, an alternative chat delivery order, an idempotent retry, and
a real, distinct `CanonicalSQLite` wrapper plus
`HistoryRepository`/`HistoryAnalyticsSource` reconstruction over the same
temporary canonical database path, key scope, and timeout. The deletion
histories restart after tombstones and before duplicate replay, stale
reappearance, and a final clean rebuild. The
`analytics_convergence_stress` (30) and `analytics_deletion_stress` (20)
profiles provide broader offline calibration. Local measurements are recorded
in the [analytics convergence evidence](architecture/analytics-convergence-local-evidence.json).
That artifact is generated from test-owned runtime instrumentation and records
actual histories, canonical mutation deliveries, clean rebuilds, repository
restarts, alternative-order histories, deletion and duplicate deliveries,
wall-clock samples, runtime versions, and an expected failure/shrink
invocation. It is not hosted-runner p95 or production-equivalent persistence evidence.

The deletion profile checks an individual message tombstone, a conversation
cascade with temporal edges, last-participant removal, duplicate deletion,
and rejected semantic reappearance through enrichments, metrics, graph nodes,
edges, referential closure, and complete materialized active publication
content. Its permanent falsifiers cover metric, provenance, identity, graph,
deletion-closure, and stale-but-current-witness active-publication corruption.

## CI coverage

GitHub Actions uses Python 3.11 and Node.js 22. In addition to the common checks, CI runs architecture boundary manifest validation, Python architecture boundary checks (`lint-imports`), Agent architecture boundary checks (`npm run check:architecture` in `extension`), Bridge architecture boundary checks (`npm run check:architecture` in `frontend`), contract-integrity tests, the provisioning-page module test, the 10,000-message Agent snapshot qualification, the backend suite on Windows, and capture end-to-end tests.

Bridge architecture boundary checks enforce acyclicity across protected protocol, store, and service namespaces (`rule-bridge-protected-acyclic`). Permitted dependency direction is governed separately by the architecture manifest.

See [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) for the current commands and job matrix.

Packaging, installed-artifact acceptance, and Beta qualification have separate checks. See [Product qualification](qualification.md) and [Windows acceptance](installation-and-acceptance.md).
