# Startup repair and final PR47 qualification

This change keeps the existing 38 jobs, hardware profiles, query samples, idle waits, public deadlines, correctness rules and latency/resource limits. It does not turn a diagnostic or an input baseline into a PASS result.

## Fixed scope and commits

1. `Instrument full analytics startup and verification counts`
2. `Reuse bounded startup verification during question readiness`
3. `Route all 38 qualification jobs with explicit prerequisites`
4. `Bind reusable inputs and document final qualification rollout`

Startup retains only bounded verified metadata. It does not persist a trust flag, copy process memory, change the graph format or database engine, or grant more time to failing work. Existing checks remain; missing or changed startup metadata takes the complete fallback.

## Verify before selecting the final candidate

The end-to-end startup test must install its counters before constructing the reopened store. For an unchanged supported active generation, it requires exactly one persisted-generation recomputation from reopening through readiness, no analyzer/product-build calls, and a correct first answer. Changing source content, publication witness, schema/trigger/content stamp, physical database identity, expiry, retention or configuration must invalidate the handoff. Cancellation must leave no partially installed proof set. Legacy compatibility and bounded memory remain tested.

The passive startup trace starts at child entry and freezes its measured interval when readiness is reached. It is written only after in-flight callbacks join. It records nested/inclusive/exclusive spans, actual observed ends for operations crossing readiness, full-recomputation/materialization counts, and uncovered time. A missing end is incomplete, not fabricated timing. `cold_readiness_seconds` remains inclusive; independent post-test verification remains separate.

Use ordinary serial admission for native tests and real-profile paired measurements. The before/after comparison uses identical safely copied 100,000-message input and distinct processes on actual four-core 8 GiB and 16 GiB profiles. No database generation is needed merely to compare startup. Preserve failed or inconclusive diagnostics. Require correct results, safety/resource checks, one computation on the stable fast path, improved inclusive readiness on both profiles and explained remaining time before promoting the candidate. Do not advertise an unmeasured absolute startup target.

## Reuse input with explicit provenance

`analytics_qualification_baselines.adopt_verified_input` accepts a donor only after both source contexts are clean and signed, the manifest/runtime/message count/variant/kind mode match, and signed fixture/storage-format inputs match. It retains the original baseline/source records and original build duration. It copies the closed database set to a distinct directory, then runs the new source's independent canonical reference computation and full persisted-content verification. It does not rely on the optimized runtime handoff for that independent check.

The independent result must equal the donor's recorded result. Source identity and donor hashes are checked again before sealing. The new producer explicitly records a verified-copy preparation method and zero initial analytics builds. A changed/incompatible donor fails rather than having its hash silently renamed. Build a new baseline only where that failure establishes it is necessary. Never copy an old PASS, warm process cache, browser profile or consent session into a new candidate's evidence.

## Preflight every route

The authoritative set is `analytics_qualification.required_jobs(manifest)`, not a separately maintained question-only list. The campaign router contains all six existing collector families:

| Family | Jobs | Existing collector |
| --- | ---: | --- |
| Questions | 24 | `analytics_qualification_runner.run_source` with `run_questions=True` |
| Packaged behavior | 2 | `analytics_qualification_packaged.run`, mode `package` |
| Packaged mutations | 4 | The same packaged collector, mode `matrix` |
| Packaged visibility | 6 | The same packaged collector, mode `visibility` |
| Regression | 1 | `analytics_qualification_runner.run_regressions` |
| Source CI | 1 | `analytics_qualification_runner.run_ci` |

`--campaign-plan` prints all routes without starting a worker. `--campaign-template` writes exact-source/manifest bindings and null prerequisite references; it never invents accepted setup or authentication. Populate the template only with actual reviewed files and their SHA-256 hashes. It has exactly one entry for every mandatory job.

For questions, bind the real hardware handoff. For each packaged job, bind its actual package descriptor, hardware handoff and separately reviewed setup record. No two packaged jobs may share the selected writable data/browser directories. Setup review is not a substitute for runtime extraction/source checks, genuine authorized capture, UI checks and actual lifecycle evidence, all of which the existing public collector still enforces.

The historically reserved onboarded installation permits only its reviewed job. It does not grant setup approval for the other eleven packaged jobs. Obtain genuine normal setup/consent and job-specific reviews where required; do not clone the reserved consent state. Missing setup is BLOCKED and must be surfaced before the final long run.

Source CI requires a review bound to the final source and actual GitHub authentication in the same guest execution context. A successful host workflow is not the source-CI receipt. The source-CI collector still obtains the exact required workflow/job evidence itself. Regression must execute its actual test groups, not be credited from development diagnostics.

## Public execution interfaces

Commands below are forms, not valid ready-made job inputs. Resolve actual paths from the final deployment and use the existing native serial owner. Never wrap a dispatcher twice.

```powershell
python tools/qualify_analytics_baseline.py --closure --output <closure> --campaign-plan
python tools/qualify_analytics_baseline.py --closure --output <closure> --resume --campaign-template <new-template.json>
python tools/qualify_analytics_baseline.py --closure --output <closure> --resume --campaign-inputs <reviewed-inputs.json> --profile <actual-profile> --preflight-campaign
python tools/qualify_analytics_baseline.py --closure --output <closure> --resume --campaign-inputs <reviewed-inputs.json> --profile <actual-profile> --run-job <exact-required-job-id>
python tools/qualify_analytics_baseline.py --closure --output <closure> --resume --campaign-inputs <reviewed-inputs.json> --profile <actual-profile> --run-campaign
```

`--run-job` is the host coordinator's one-job dispatch seam. It refuses an unknown tuple and does not create a second attempt for an already attempted tuple. `--run-campaign` runs jobs whose actual profile and prerequisites are ready; blocked jobs remain explicitly listed and cannot yield aggregate PASS. The host coordinator must perform actual normal VM profile transitions and supply the next reviewed configuration. It must not manufacture profile evidence or treat a route being present as proof that setup is complete.

The first constrained populated/fresh result remains the initial gate. The route order then exposes reference populated/fresh and reference visibility early, followed by stable profile groups. Missing references do not create failed duplicate attempts: preflight reports them before dispatch. An existing failed/unaccepted attempt requires review; the router does not retry it unchanged. A public failure stops dispatch. Accepted tuples are read from real public evidence and never rerun automatically.

For the public in-process campaign, the stop file is `<closure-parent>/<closure-name>.stop-after-current`, outside the immutable closure. Existing private host controllers retain their own external `STOP_AFTER_CURRENT` contract. At a stop boundary, finish the current job, preserve/export complete evidence, verify native joins and use normal shutdown/release. Never edit a running source/input or restore over unexported evidence.

## One final candidate and merge gate

Finish code, instrumentation, tests, CI-tier/shard metadata, documentation and adapter preflight before publishing the intended final signed candidate F. Sign as the existing ADO-agent identity. Update PR47 directly without changing main. Build the exact F artifacts, run required F CI including the scale workflow, and bind the guest source/runtime/manifest/artifacts and all executed private helpers explicitly. No post-start housekeeping commits to alter the frozen source digest.

Existing 08b/2cef/ab3625e results remain historical evidence for their actual builds. A runtime-changing F begins with zero accepted F jobs; verified input adoption is separate from result credit. Use persistent baselines and content-addressed incremental export so progress does not regenerate the same databases or copy all historical data after each job.

Before a packaged installation/checkpoint transition, preserve the latest complete closure and every referenced object. Afterward restore that verified state, never the initial closure. Keep the real task/owner creation identities and shared admission; a VM merely being powered on is not progress. If a test passed but export failed, repair export without rerunning it. Preserve genuine failures.

Completion requires the unchanged public aggregate verifier to return all 38 mandatory jobs PASS and evidence validity PASS for exact F and its artifacts. Then check that PR47 still points to F, required CI and normal reviews are satisfied, and perform a head-guarded normal merge. Do not merge on 24 question results, a mixed-source count or a monitor status. Verify the merge and retain the final receipts before disabling the single continuation monitor.
