<!-- CODE-VERIFY: Check qualify_analytics_baseline.py, pytest.ini, test profiles, question fixtures, and packaging scripts before changing commands or qualification claims. -->

# Qualify analytics changes

Run contract checks and regression tests separately from language quality, performance, and installer qualification. Passing one does not establish the others.

## Regression baseline

Use Python 3.11 with the pinned development requirements, native Noise module, and the platform's required SQLCipher runtime. Windows tests use the pinned Windows wheel. Use a checkout that Git can identify from the selected Python runtime. Build frontend assets before collecting tests that import the application. Give each test invocation a dedicated temporary directory.

```powershell
npm ci --prefix frontend
npm run build --prefix frontend
python -m pytest tests/test_analytics_question_cases.py tests/test_analytics_baseline_qualification.py --basetemp C:\temp\analytics-contract-tests
python tools/qualify_analytics_baseline.py --output C:\temp\analytics-baseline
```

The output directory must be new. The runner records source hashes, runtime versions, exact test selections, fixed Hypothesis seed, profiles, timings, exit codes, and JUnit counts. Logs and reports stay local unless reviewed for sharing. An incomplete or timed-out run cannot qualify the baseline.

The baseline covers analytics, graph storage, publication, authorization, retention, deterministic rebuilds, general convergence, deletion convergence, and injected faults. Linux source tests supplement native Windows checks; neither is packaged-runtime or laptop-capacity evidence.

Question fixtures use manually specified answers and an independent structural validator. They do not execute a question service. A query implementation must pass these literal cases plus endpoint authorization, pagination, generation-change, work-limit, and source-resolution tests before activation.

## Language quality

Pricing discussions may be activated only after held-out, authorized, representative examples establish at least 90% precision within the declared language and scope. Report sample counts, confidence intervals, recall, abstention, and performance by relevant input group. A tiny or unrepresentative set cannot qualify the feature even when its point estimate passes.

Freeze annotation guidance, data split, taxonomy, model/rule version, and thresholds before final evaluation. Separate participants/accounts and time where possible. Include slang, ambiguous keywords, quoted text, negation, emoji, and unsupported languages. Review disagreements rather than replacing them with model labels.

Synthetic fixtures establish mechanics, not natural-language quality. The repository question cases contain classifier outputs, not an annotated conversational corpus. Keep pricing disabled or explicitly narrow its meaning until the relevant quality gate passes. No model family is preselected.

## Workload and measurement

Use the CPU-only laptop profiles and pack budgets in [Local analysis](local-analysis.md). Record actual OS, CPU model, core/thread limits, RAM, free memory, disk type, runtime versions, power mode, source revision, and enabled analyzers. A faster desktop result does not qualify a laptop.

Exercise 10,000 and 100,000 retained messages. Treat 1,000,000 as an opt-in stress case, not supported capacity. In each profile, put half the messages in one conversation and distribute the remainder across 100 conversations. Use deterministic synthetic content and alternate message direction. Place event times uniformly over the 48 hours preceding a fixed evaluation clock, with stable tie cases tested separately.

Measure a cold build, unchanged rebuild, a single new message, 100 edits, 100 deletions, and a 10,000-message historical batch interleaved with 100 live messages. Record exact source counts and revisions before and after each mutation. Compare resulting semantics with the fixed cases and clean rebuild checks.

Report canonical ingest time, enrichment time, graph construction, publication, end-to-end visibility lag, process-tree peak memory, and backlog drain rate separately. Keep test preparation outside measured intervals. Record failed and cancelled operations; do not discard them from the report.

Once question handlers exist, measure 100 warm calls per approved question after five warm-up calls, with page size 50 and fixed filters. Report nearest-rank p95, maximum, errors, scanned records, and truncation. The initial 100,000-message reference targets are p95 at most one second and one committed message visible within ten seconds without backlog. These are acceptance targets, not current results.

Before distributing an optional model, compare the same application build with and without its pack. Record compressed installer size, unpacked bytes, runtime and model download closure, temporary disk needs, cold/warm inference, and peak memory. Missing measurements are null with a reason, never zero. A source-only test run cannot establish installer size or frozen-runtime compatibility.

## Integration gates

| Gate | Required evidence | Responsible role |
|---|---|---|
| Source context | Gateway supplies available coverage, order evidence, event kind, and source version; unavailable information stays unknown. | Analytics/backend |
| Pricing quality | Approved annotation guidance and held-out representative data pass the declared task gate. | Applied ML and product |
| Interactive performance | Executed saved queries and update workloads on the recorded laptop profiles. | Analytics/backend and testing |
| Distribution | Measured base installer and optional pack closure, verification, cancellation, removal, and offline operation. | Packaging/security and testing |

These gates remain requirements even when structural fixtures and regression suites pass. They do not authorize a capture redesign, customer-data upload, or a new inference runtime.
