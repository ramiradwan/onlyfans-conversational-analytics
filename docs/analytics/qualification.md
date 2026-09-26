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

## Frozen A07 closure

[The acceptance manifest](acceptance-manifest.json) is the authority for closure. It fixes the two Windows profiles, fixtures, questions, repetitions, clocks, limits, and required evidence. The ten-second visibility and one-second warm-query targets apply to the 16 GiB reference profile. The 8 GiB constrained profile runs the same correctness and stability work and reports its measured latency. Bulk work and cold readiness have completion guards, not interactive latency promises.

Use the existing baseline command. Each output directory belongs to one exact source, interpreter, dependency set, and manifest:

```sh
python tools/qualify_analytics_baseline.py --closure --output /path/to/new-evidence
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --resume --run-regressions
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --verify
```

The verifier returns 0 for PASS, 1 for FAIL, and 2 for BLOCKED. It recomputes the verdict from raw records; a summary marked successful is not authority. Every started attempt must have a durable result. Failures remain failures when the directory is resumed. A new source or protocol requires a new directory, not replacement of earlier records.

The runner owns its process tree on Linux and Windows. Windows workers wait until assigned to a kill-on-close job. Linux workers use a process group and a parent-death pipe. Cancellation, watchdog expiry, and children left running are failures. Measurement v3 also waits for Windows process objects to signal completion; a zero job counter alone is not a joined worker. The earlier v2 shutdown-test failure remains recorded. The owner lock excludes overlapping runs using that lock. All agents on the same benchmark machine must use the same owner lock and avoid unrelated benchmark work.

### Source diagnostics

The matrix, visibility, and question collectors use the ordinary scheduler and query service over isolated synthetic stores. They are source diagnostics, not packaged application, UI, authorized ingestion, or laptop qualification. A successful source diagnostic cannot make a packaged gate PASS.

```sh
python tools/qualify_analytics_baseline.py --closure --output /path/to/matrix --run-source matrix --messages 100000
python tools/qualify_analytics_baseline.py --closure --output /path/to/visibility --run-source visibility --repeat 0
python tools/qualify_analytics_baseline.py --closure --output /path/to/questions --run-source questions --case populated --state idle --known-synthetic-kinds
```

Run the matrix at both manifest sizes. Visibility repetitions are numbered 0, 1, and 2. Each starts a new interpreter and exercises its ordinary, unchanged-rebuild, idle, and restarted cases. The restarted case uses another interpreter after the previous scheduler joins. Question cases are `populated`, `empty`, `tied_time`, and `generation_bound_pagination`; each has `fresh`, `idle`, and `mutated` states. Every question sample set has one observed runtime identity, five warm-ups, and 100 measured calls.

`--known-synthetic-kinds` supplies known message kinds only to the diagnostic query fixture. It tests question mechanics without changing production unknown-kind handling or request budgets. It does not establish that ingestion supplies those kinds. Pricing stays disabled. Without this option the diagnostic retains production unknown-kind behavior and may fail the declared populated-answer expectations.

`--subject-root` selects a separate Git checkout for a historical runtime control. Its full file hashes and revision are recorded independently of the runner. An older source cannot substitute for the final source. Preparation always happens inside the owned worker; there is no prepared-store shortcut.

### Clocks, evidence, and closure

The versioned measurement protocol records durable canonical commit, activation, first observed current question result, required publication cleanup, and backlog drain separately. The visibility interval ends only when the visible result, cleanup, and drain have all completed. Operation records are saved before independent rebuild verification; completed verification gets its own durable phase record. A killed verifier cannot erase a completed operation or qualify a missing phase.

The original 1,800-second whole-worker limit remains. It includes fixture preparation, startup, independent verification, and synchronous product cleanup. Expiry does not identify an individual operation's latency. The new protocol adds observed query visibility and owned-process-tree accounting; it does not reinterpret the old interrupted matrix as passing. Windows resource records distinguish job peak private commit from Linux sampled process-group resident memory. Neither is a packaged memory receipt.

The first session records infrastructure preflight. Missing guests, permissions, package inputs, or a packaged UI/ingestion adapter leave those gates BLOCKED. No guest permission is changed. Package/profile adapters must supply the same raw source, process, fixture, artifact, and phase bindings before those gates can pass; source fixture writes cannot be relabelled authorized ingestion.

After reviewing an exact clean source, save a review record with `source_revision`, `source_sha256` (the digest of `source_context`), `reviewer`, and `checks`. `--run-ci --review-record /path/to/review.json` reads the six named checks through the authenticated `gh` command. It retains raw API responses, selects the latest attempt for each named check, and requires success on that exact SHA. Run this after CI has finished; pending or failed checks are not passes.

A07 closes only when every mandatory gate passes for the final signed source and exact package. A material runtime change requires rerunning affected qualification. Missing package or profile evidence is qualification blocked, not engineering completion or qualified done. The five planned acceptance units do not authorize an automatic sixth optimization pass.
