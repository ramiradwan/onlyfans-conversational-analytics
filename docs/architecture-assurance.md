# Architecture assurance baseline

This page records the repository's executable architecture-assurance baseline.
The authoritative definitions live in
[`architecture-boundaries.json`](architecture-boundaries.json); this page
summarizes their coverage and identifies qualifications that require hosted
CI or release evidence.

`tests/test_architecture_baseline.py` derives the counts below from the
manifest, tracked repository paths, Import Linter contracts, executable
controls, and generated evidence. Unknown production paths fail validation.
Each qualified semantic invariant names both executable evidence and a
permanent falsifier. A related test alone does not promote an invariant from
`documented` to `qualified`.

The Python composition contracts enforce these ownership boundaries:

- the persistence factory returns persistence-owned resources;
- transport does not construct the canonical analytics source;
- application services do not discover transport dependencies;
- bootstrap constructs and injects the canonical analytics source; and
- `CanonicalRepositories` exposes no ingestion service.

Agent and Bridge dependency checks reject cycles through their protected
kernels. Those checks establish acyclicity. Permitted dependency direction is
defined separately by the manifest and language-specific contracts.

The sole architecture exception is the current composition seam from
`app/persistence/projection_activation.py` to `app.analytics`, identified as
`DESIGN-PROJECTION-ACTIVATION-IDENTITY`. There are no temporary exceptions.
Validation rejects missing, expired, duplicate, untracked, or out-of-scope
exception entries.

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

The negative-control count is rule-scoped because one Agent dependency test
module covers two rules. Run the focused baseline checks with:

```powershell
python -m pytest --override-ini=addopts= --basetemp=.pytest_temp_architecture tests/hardening/falsifiers/test_falsifiers.py tests/test_architecture_baseline.py tests/test_architecture_contracts.py tests/test_architecture_boundaries.py tests/test_architecture_runtime_policy.py tests/test_architecture_admission.py tests/test_docs.py
npm test --prefix extension -- tests/architecture-boundaries.test.mjs
npm test --prefix frontend -- tests/architecture-boundaries.test.ts
python tools/check_docs.py
```

The baseline test parses this table and compares every label and value with
the current manifest and source controls, so documentation drift fails CI.

## Hosted qualification still required

Repository-local controls do not establish hosted-runner performance or
packaged Windows runtime behavior. Release qualification requires:

- a representative hosted pull-request sample reporting baseline size,
  p50, p90, p95, and the added critical-path p95 of the generated ingestion
  and analytics suites; the target is at most 5%; and
- successful hosted Windows CI and release-package runs that retain the fixed
  SQLCipher wheel, provenance, runtime probe, and frozen executable evidence.

The checked-in local evidence records actual profiles, transitions,
restart/replay or convergence measurements, runner versions, and falsifier
configuration. It must not be presented as hosted or release-equivalent
evidence. Production traces are optional calibration inputs; repository-derived
requirements and independent assurance models define correctness.
