# Prepared question inputs

The `verified-question-inputs.v1` collector separates synthetic input preparation from question execution. It does not change the 38 required jobs, hardware profiles, sample counts, 61-second idle wait, latency gates, or worker/resource limits.

## Build only the distinct starting inputs

For each exact source/runtime/manifest/message-count combination, prepare an `ordinary` baseline for populated, empty and pagination cases, plus a `tied_time` baseline. Fresh, idle and mutated jobs receive isolated copies. The 24 question jobs therefore need at most two initial baseline builds shared across both profiles, not 24. Missing inputs are built; corrupt existing inputs are rejected rather than silently rebuilt.

The source hash covers application, collector and fixture code. Runtime, complete manifest (including fixture seed and evaluation clock), message count, variant and synthetic-kind mode are part of the baseline identity. Each new baseline is independently reconstructed from canonical data and checked against stored analytics. Before sealing, the fixture closes its work, confirms zero open connections and checkpoints the encrypted database set. A busy checkpoint or remaining journal data prevents publication. Initial migration backups and logs are retained once as preparation evidence, not copied into every job. The baseline contains the canonical and analytics databases, the repository’s small legacy-projection database, and its preparation manifest, never a running-process/VM-memory snapshot.

The hardware profile does not alter these deterministic input databases. Their originating profile is recorded, but it is not part of the input cache key. Each job must still independently establish and measure its actual execution hardware.

## Keep the operations under test

Every job hashes and copies the baseline into a new private directory and starts a fresh application process. No hardlinks, shared writable databases or process caches are reused. Normal storage recovery/readiness, real idle waits, committed edits, pagination invalidation, query samples, correctness checks and joined cleanup remain.

Unmodified fresh/idle cases rescan canonical content and validate stored analytics against the independently verified baseline expectations. They do not calculate the same expected analytics again. Mutated and pagination cases independently reconstruct expected analytics after their actual edits. Changed canonical or stored-content digests fail.

Cold build/rebuild, mutation, visibility, ingestion and packaged-setup tests still perform the actions they qualify. The new baseline path applies only to synthetic question jobs. It cannot substitute for authorized packaged ingestion or consent.

## Working data and evidence

Shared baselines live under `<closure>/question-baselines/<baseline-id>/`. Per-job copies live in that job's own `attempts/<attempt-id>/collector/data/` directory, on the storage observed by the hardware evidence. Before the result is sealed, successful copies are removed only after correct answers, verification and joined cleanup; their ownership marker must match the job receipt. Failed/incomplete data stays with the attempt and is included in its evidence export. No data from an earlier completed attempt is deleted.

Each job records its exact baseline manifest, whether it built or reused input, preparation/copy times, fresh process identity, readiness time, query samples, verification mode and cleanup result. The original build time remains distinct from the current job's preparation time. Reusing data does not reuse a PASS.

`tools.analytics_qualification_bundle` provides nonduplicating transport for closed evidence. Objects are stored by SHA-256. Each manifest binds its parent and every file's path, length and hash. Subsequent exports copy only new objects. Changed or missing prior evidence is rejected. Restore verifies every object and recreates the original tree. The bundle is transport, not an acceptance verdict.

From the candidate source directory:

```powershell
python -m tools.analytics_qualification_bundle export --closure C:\qualification\closure-new --store D:\qualification-evidence
# Later exports add --parent <previous manifest_sha256>.
python -m tools.analytics_qualification_bundle restore --store D:\qualification-evidence --manifest-sha256 <actual manifest_sha256> --output C:\qualification\restored-closure
```

Keep the object store outside the closure and retain every referenced object. Run only after producers join and under ordinary serial admission. The existing public verifier still determines whether restored evidence passes.

## Diagnostic and rollout

`python -m tools.diagnose_question_preparation --output <fresh-directory>` compares the legacy ordinary result with prepared-input results in actual owned child processes. It exercises ordinary/empty/idle/mutated/pagination/tied cases and expects two initial preparations for seven prepared jobs. It uses 400 messages, three measured calls and a short idle wait; it carries zero qualification credit.

Roll out as a new candidate with newly bound source/runtime/manifest inputs. Do not hot-patch an active worker, reinterpret old-protocol results, or mix changed source identities into the old campaign. Preserve the original frozen checkout and accepted evidence. Stage the revised collector and transport in the real adapters before claiming deployment. An unchanged private cumulative-ZIP wrapper will still duplicate transfers; use the bundle transport or preserve the same closure between jobs.
