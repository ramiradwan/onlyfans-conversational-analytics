# Architecture assurance baseline

[`architecture-boundaries.json`](architecture-boundaries.json) defines the architecture baseline. This page records its checked repository state and remaining hosted checks.

`tests/test_architecture_baseline.py` derives the counts from the manifest, tracked paths, Import Linter contracts, controls, and evidence. Validation rejects unknown production paths. A qualified semantic invariant must name executable evidence and a permanent falsifier.

Python composition checks require:

- persistence factories to return persistence-owned resources;
- bootstrap to inject the canonical analytics source;
- transport and services to avoid constructing or discovering analytics dependencies; and
- `CanonicalRepositories` to expose no ingestion service.

Agent and Bridge checks reject cycles in protected modules. The manifest and language-specific contracts define allowed dependency direction.

The current-design exception `DESIGN-PROJECTION-ACTIVATION-IDENTITY` permits the composition seam from `app/persistence/projection_activation.py` to `app.analytics`. There are no temporary exceptions.

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

One Agent dependency test covers two rules, so the negative-control count is rule-scoped. Run the focused checks with:

```powershell
python -m pytest --override-ini=addopts= --basetemp=.pytest_temp_architecture tests/hardening/falsifiers/test_falsifiers.py tests/test_architecture_baseline.py tests/test_architecture_contracts.py tests/test_architecture_boundaries.py tests/test_architecture_runtime_policy.py tests/test_architecture_admission.py tests/test_docs.py
npm test --prefix extension -- tests/architecture-boundaries.test.mjs
npm test --prefix frontend -- tests/architecture-boundaries.test.ts
python tools/check_docs.py
```

The baseline test compares this table with current controls and fails on drift.

## Hosted checks

Repository-local checks do not establish hosted performance or packaged Windows behavior. Release evidence requires:

- hosted pull-request measurements for baseline p50, p90, p95, and added critical-path p95, with a target increase of at most 5%; and
- hosted Windows and release-package runs retaining the fixed SQLCipher wheel, provenance, runtime probe, and frozen executable evidence.

Checked-in local evidence records executed profiles, transitions, measurements, versions, and falsifier settings. It is local evidence only. Repository requirements and independent models define correctness; production traces may provide calibration data.
