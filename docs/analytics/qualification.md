<!-- CODE-VERIFY: Check qualify_analytics_baseline.py, analytics_qualification*.py, pytest.ini, question fixtures, packaging scripts, local-analysis.md and acceptance-manifest.json before changing commands or qualification requirements. -->

# Qualify analytics changes

The [acceptance manifest](acceptance-manifest.json) defines the workload, Windows profiles, execution budgets and evidence required for qualification. Regression, semantic query and packaged behavior results establish different properties.

## Regression baseline

Use Python 3.11 with pinned development dependencies, the native Noise module and the required SQLCipher runtime. Windows tests use the pinned Windows wheel. Git must identify the checkout from the selected interpreter. Build frontend assets before tests that import the application, and give each invocation a dedicated temporary directory.

```sh
npm ci --prefix frontend
npm run build --prefix frontend
python -m pytest tests/test_analytics_question_cases.py tests/test_analytics_baseline_qualification.py --basetemp /path/to/new-test-directory
python tools/qualify_analytics_baseline.py --output /path/to/new-baseline-evidence
```

The baseline records source hashes, runtime versions, exact test selections, fixed Hypothesis seed, profiles, timings, exit codes and JUnit counts. Reports stay local until reviewed for sharing. An incomplete or timed-out run cannot qualify. Coverage includes analytics, graph storage, publication, authorization, retention, deterministic rebuilds, convergence, deletion and injected faults. Linux source tests supplement native Windows checks without establishing packaged behavior or laptop capacity.

Question fixtures contain manually specified answers checked by an independent structural validator. Enabling a query also requires endpoint authorization, pagination, generation-change, work-limit and source-resolution checks.

## Language quality

Enable pricing discussions only after held-out, authorized, representative examples establish at least 90% precision for the declared language and scope. Report sample counts, confidence intervals, recall, abstention and performance by relevant input group. A small or unrepresentative set cannot qualify the feature solely through its point estimate.

Freeze annotation guidance, data split, taxonomy, model or rule version, and thresholds before final evaluation. Separate participants, accounts and time where possible. Include slang, ambiguous keywords, quoted text, negation, emoji and unsupported languages. Review annotation disagreements without replacing them with model labels.

Synthetic fixtures establish query mechanics. The repository cases contain classifier outputs and do not constitute an annotated conversation corpus. Keep pricing disabled or narrow its declared scope until the applicable quality gate passes. No model family is preselected.

## Workload and measurement

Use the manifest's Windows virtual-machine profiles with four virtual processors and 8 GiB or 16 GiB static memory. Record guest and host CPU models, power modes, virtualization, RAM, free memory, storage evidence, runtime versions, source revision and enabled analyzers. These measurements do not qualify physical-laptop performance or advertised minimum requirements. Optional model-pack budgets remain in [Local analysis](local-analysis.md).

Exercise 10,000 and 100,000 retained messages. The 1,000,000-message workload is an optional stress case. Put half the messages in one conversation and distribute the remainder across 100 conversations. Use deterministic synthetic content, alternating direction and event times spread across the 48 hours before a fixed evaluation clock. Exercise stable timestamp ties separately.

Measure a cold build, unchanged rebuild, one new message, 100 edits, 100 deletions and 10,000 historical messages interleaved with 100 live messages. Record source counts and revisions around each mutation. Compare semantic results with literal cases and an independent full rebuild.

Report canonical ingestion, enrichment, graph construction, publication, end-to-end visibility, process-tree peak memory and backlog drain rate separately. Preparation stays outside operation latency and inside its execution guard. Retain failed and cancelled operations.

For each enabled question, collect 100 warm calls after five warm-ups, with page size 50 and fixed filters. Report nearest-rank p95, maximum, errors, scanned records and truncation. At 100,000 messages the reference profile requires warm p95 at most one second and each committed-message visibility probe at most ten seconds, including cleanup and drained backlog. The constrained profile preserves correctness and stability checks and reports latency without these reference-profile limits.

Before distributing an optional model, compare the same application build with and without its pack. Record compressed installer size, unpacked bytes, runtime and model download closure, temporary disk needs, cold and warm inference, and peak memory. Record unavailable measurements as null with a reason. Source tests cannot establish installer size or frozen-runtime compatibility.

## Integration gates

| Gate | Required evidence | Responsible role |
|---|---|---|
| Source context | Available coverage, ordering evidence, event kind and source version retain their meaning. Missing metadata stays unknown. | Analytics and backend |
| Pricing quality | Approved annotation guidance and held-out representative data pass the declared task gate. | Applied ML and product |
| Interactive performance | Saved queries and update workloads execute on both declared virtual-machine profiles. | Analytics, backend and testing |
| Distribution | Base installer and optional pack measurements cover dependencies, verification, cancellation, removal and offline operation. | Packaging, security and testing |

These requirements remain applicable when structural fixtures and regression tests pass. Qualification does not authorize changing capture scope, uploading customer data or introducing another inference runtime.

## Prepare the source and evidence

Use the pinned dependencies and native runtimes, build the frontend, and review one clean signed source revision. Use a new evidence directory outside the repository. Every receipt binds that source, the interpreter, dependencies and manifest. A changed source or protocol requires a new directory.

Use the same host-admission launcher and owner lock as other work on the qualification machine. Missing prerequisites are BLOCKED. Started work that fails, times out or loses its receipt is FAIL. Resuming preserves every attempt and cannot erase a failure.

Before starting expensive workloads, run the fast qualification protocol tests and a native timer preflight on the pinned guest interpreter through that launcher. The preflight exercises both asynchronous and synchronous waits against the manifest's measured idle minimum, records source and runtime bindings, and gives no qualification credit. These controls catch timer and harness errors; they do not replace the complete workload matrix.

```sh
python -m pytest tests/test_qualification_prepared_inputs.py tests/test_qualification_process_improvements.py tests/test_light_first_update_benchmark.py tests/test_analytics_closure_qualification.py --basetemp /path/to/new-preflight-test-directory
python -B -m tools.analytics_qualification_idle --source-root /path/to/signed-source --output /path/to/new-timer-preflight.json
```

```sh
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --package-inputs /path/to/package-inputs.json
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --resume --run-regressions
```

Package inputs identify immutable installer and runtime artifacts plus their extracted files, a dedicated provisioned installation and a browser profile authorized by the unchanged package. Keep these paths and authority material outside the repository. Do not replace package trust, seed authentication databases or write fixture records directly into packaged canonical storage.

The private input document uses schema `analytics-package-inputs.v1`. Set `source_revision` to the signed revision. `artifacts.installer` and `artifacts.runtime` each contain an absolute `path` and its `sha256`. The runtime ZIP contains `Brain.exe`, its build-produced `release-manifest.json` and the unchanged Agent, and `runtime_directory` matches every archive member. Qualification checks the archived manifest's `source_commit` against the signed revision during preflight and final verification. Set `agent_directory` within that directory, `data_directory`, `browser_profile` and the dedicated `synthetic_account_id` value `synthetic-continuous-owner`. The installation must already hold a valid authority grant and full consent through its ordinary setup flow.

Set `bridge_origin`, the optional `bridge_path`, `platform_origin`, `identity_path`, `conversations_path` and `messages_path_prefix` from the packaged adapter's supported upstream contract. The browser supplies only deterministic synthetic responses to those upstream reads. An optional `browser_executable` selects an installed browser. No credential or signing key belongs in the input document. Keep explicit canonical and analytics database paths in the installation's private `runtime.env`, within its data directory, with its existing local encryption key.

Use a fresh, empty dedicated installation for each packaged job. The guest must have no active non-loopback network adapters or default routes, with dependencies and valid grants available locally. The collector measures these facts before launch and after cleanup. It reports unavailable hardware, artifacts, browser dependencies or isolation as BLOCKED before starting product work. It preserves any started failure as FAIL.

### Verify virtual storage

Pass `--hardware-handoff /path/to/handoff.json` for semantic questions and packaged jobs. The private document uses schema `analytics-hardware-handoff.v1` and supplies an existing guest `directory`, the selected `vm_id` and the reviewed host observer's `producer_sha256`. It contains no credentials. The host observer uses PowerShell Direct with that VM ID while the guest network remains disconnected.

The runner creates a fresh attempt directory inside the handoff directory. It writes `pre.request.json` before launching work and `post.request.json` after the owned worker joins. The host observer answers each with the corresponding `*.response.json`, published atomically. Each response binds the complete request digest, producer digest, guest collector digest and raw storage snapshot. Each response has a 180-second deadline. Observation time stays outside operation latency and does not extend worker execution budgets.

Supported storage has exactly one virtual SCSI boot/system disk, agreeing complete guest disk inventories and no additional virtual disks or nonprimordial storage pools. Every workload path must map to that disk without reparse redirection. Every file in its attached backing chain must map through host volume and partition associations to physical SSD storage. Unknown media, missing parents, extra disks, unsupported storage or changed topology fail verification. The guest's raw media label remains recorded even when it says `Unspecified`.

Archived evidence binds the source, manifest, attempt, guest boot, workload paths and local observations to the selected VM. Final verification repeats the joins from those records. A discovery report, scalar SSD flag or digest without its reviewed observer cannot establish this evidence. See the [hardware evidence contract](hardware-evidence.md) for the producer schema.

## Semantic questions

The semantic track executes known synthetic event kinds on each declared Windows profile. It preserves the manifest's literal answers, pagination, mutation, idle, sample-count, work-limit and latency checks. It qualifies query correctness and latency for that declared input. It provides no evidence that production capture supplies event kinds.

```sh
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --resume --run-questions --profile reference-windows-16g --case populated --state fresh
```

Run every manifest case and state on both profiles. Each sample set uses a fresh observed interpreter, five warm-ups and 100 measured calls. The reference profile enforces the declared warm p95 limit. The constrained profile records latency and retains correctness and resource requirements.

## Packaged behavior

The packaged track runs the exact artifacts through normal Agent capture, consent, pairing and authenticated delivery. Creator deletions use the authenticated Vault command. Each operation records admitted canonical commits, actual source revisions, current question results, synchronous cleanup and drained backlog. Synthetic source writes cannot substitute for authorized ingestion.

Production event kinds remain unknown. A current result reports the affected conversations as undetermined. Packaged interface checks verify that state without claiming a qualified positive no-reply list. Pricing remains unavailable until its separate language-quality gate is satisfied.

```sh
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --resume --run-package matrix --profile reference-windows-16g --messages 100000 --package-inputs /path/to/package-inputs.json
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --resume --run-package visibility --profile reference-windows-16g --repeat 0 --package-inputs /path/to/package-inputs.json
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --resume --run-package package --profile reference-windows-16g --package-inputs /path/to/package-inputs.json
```

Both workload sizes, all fresh repetitions and both profiles remain mandatory. Visibility ends at the latest current visible result, required cleanup or drained backlog. Cold and bulk work retain their execution guards. The receipt observer records actual monotonic lifecycle boundaries without changing product state. Missing events, lost buffers, unjoined processes or artifact changes invalidate the result.

The supervisor supplies separate inherited pipes for passive receipts and parent lifetime. Closing the parent-owned lifetime pipe requests ordinary application shutdown, including for the windowed executable. It sends no commands through the receipt observer. A successful exit and complete joined-worker receipts are both required.

For a cold full-size build, seed through admitted capture, close the process, launch the same package in a fresh process and request a full rebuild. Timing starts at process launch and requires the post-request generation and drained scheduler. The persisted predecessor remains present. This measures fresh-process full recomputation, with no claim about an empty store or cold disk cache. Independent read-only verification runs after the operation endpoint and within the same state budget. It compares all three persisted digests with a fresh canonical rebuild, checks obsolete generation access and validates retained storage.

The independent fixture also checks admitted message identities, conversations, senders, text, timestamps and direction after every mutation. Evidence stores the matching counts and digests. Working-only messages remain in scope without requiring Vault retention. Package cancellation requires a receipt from an interrupted full analytics build, joined shutdown and recovery in a fresh process. A build that completes before cancellation does not satisfy this check.

## Source diagnostics

`--run-source matrix`, `--run-source visibility` and `--run-source questions` remain diagnostic commands. `--known-synthetic-kinds` changes only the source query fixture. Profiling and continuation after a failed visibility probe cannot qualify latency. Component and shortened workload results cannot replace packaged evidence.

## Review and verify

Save a review record with `source_revision`, the digest of `source_context` as `source_sha256`, `reviewer` and `checks`. Collect CI after every named manifest check finishes successfully on that exact source. The collector retains raw responses and selects the latest attempt for each check.

```sh
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --resume --run-ci --review-record /path/to/review.json
python tools/qualify_analytics_baseline.py --closure --output /path/to/evidence --verify
```

Verification recomputes the verdict from immutable raw evidence and rechecks source and artifact hashes. Exit codes are 0 for PASS, 1 for FAIL and 2 for BLOCKED. Final acceptance requires every mandatory gate for the final signed source and exact package. A material change requires repeating affected qualification.


## Reusable synthetic question preparation

New campaigns use `verified-question-inputs.v1` from the acceptance manifest. The collector builds and independently verifies two input variants shared across profiles, then gives each question job a private copy and a new process. Unmodified jobs rescan and validate against the verified reference; mutation/pagination jobs still independently rebuild their changed expected outputs. See [Prepared question inputs](prepared-question-inputs.md) for storage, integrity, diagnostics and rollout. Old-protocol results and frozen source contexts are not rewritten.
