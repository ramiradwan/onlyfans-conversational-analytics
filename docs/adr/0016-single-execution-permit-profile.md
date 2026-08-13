# ADR 0016: Define the single-execution permit profile

- Status: accepted

## Context and problem statement

ADR 0014 defines the common local authorization foundation and explicitly leaves execution-state semantics to capability profiles. This decision defines the profile for a signed object that authorizes one local execution.

The profile needs atomic reservation, restart-safe execution, atomic output commitment and spend, deterministic replay behavior, and durable access to a committed contribution. Those rules apply only to this profile and do not define the shape of other capability execution.

## Decision drivers

- Admit exactly one execution for one permit without widening identity or account scope.
- Reserve the permit atomically with its durable job.
- Commit the canonical contribution and spend atomically.
- Resume retries before output without a second admission.
- Release a reservation only after a terminal pre-output failure.
- Keep completed contributions readable and projectable without another permit.
- Preserve the published decision-table meaning and request idempotency.
- Verify the permit without a local-clock dependency.

## Decision outcome

Choose **a purpose-separated `SingleExecutionPermit` with durable `available -> reserved -> spent` state in `canonical.sqlite3`**.

### Purpose-separated signed object

`SingleExecutionPermit` names the existing, immutable `capability-permit-v1` contract: protected-header `typ` and payload `profile` equal to `urn:bridge-clean:capability-permit:v1`, audience `urn:bridge-clean:local-brain:capability-permit`, a closed payload schema, and signing purpose `capability-permit`. None of these is reused by the ADR 0015 `CapabilityLicense` profile (signing purpose `capability-license`), an ADR 0008 grant, or another signed-object category.

The subject is the canonical tuple of organization, installation, and capability. Subject components are encoded in the contract-defined order and form and equal the corresponding closed-schema fields byte for byte.

> [!NOTE]
> *Canonical* here is the encoding sense defined in ADR 0014, not the storage sense.

The closed schema contains immutable `permit_id`, `issuance_id`, `organization_id`, `installation_id`, `installation_key_id`, installation-key thumbprint, capability identifier, `catalog_version`, `credit_cost`, and `execution_limit = 1`. Unknown fields fail schema validation.

The permit has no expiry, not-before boundary, or other validity window. An issuance field may be retained as signed provenance, but verification does not compare it with the local clock.

### Policy composition and scope

A verified permit contributes `SingleExecutionAdmission` to `RuntimePolicy` only when:

- signature, protected type, profile, purpose, closed schema, issuer, audience, and subject validate;
- `organization_id` exactly equals the organization in every independently verified signed object and local binding used by the decision and in `RuntimePolicy.identity`;
- installation identifier, installation key identifier, and installation-key thumbprint exactly match the enrolled installation;
- capability matches the requested execution; and
- `execution_limit` is exactly one and local admission state permits the requested event.

The permit supplies no principal, role, account membership, or account binding. It cannot substitute for an ADR 0008 grant or widen identity, organization, or account scope. The creator account comes from the independently authorized identity subset of `RuntimePolicy`; neither import nor admission selects an account.

### Persistence authority

| File | Single-execution authority |
| --- | --- |
| `auth.sqlite3` | Verified signed-object reference and digest, pinned trust-set state, authenticated key-transition state, and revocation state |
| `canonical.sqlite3` | Confirmation records, admission state, durable jobs, parameter digests, contribution links, contribution records, and deletion tombstones |
| `projections.sqlite3` | Rebuildable presentation, indexes, graph overlays, aggregates, and read models derived from canonical contributions |

Reserving an available admission and creating its durable job record is one transaction in `canonical.sqlite3`. This preserves ADR 0009's rule that no correctness property depends on a transaction spanning SQLite files.

Committing the complete canonical contribution, changing the admission from `reserved(job_id)` to `spent`, and linking permit, job, and contribution is a second single transaction in `canonical.sqlite3`. No partial combination of contribution, spend, or link is visible.

### Permit import

The delivery boundary and explicit local import path are defined by ADR 0014. `permit_id` is installation-unique across every permit state. Import stores the verified compact-JWS digest and parsed reference in `auth.sqlite3` as `pending_admission`, idempotently inserts the matching `available` admission and signed-object digest in `canonical.sqlite3`, and changes the auth reference to `active` only after verifying the canonical row.

Only an active auth reference with an exact matching canonical row can contribute admission authority. A crash leaves fail-closed staged state that byte-identical reimport or startup reconciliation completes forward.

Reimporting byte-identical content, or content with the same `permit_id` and compact-JWS SHA-256 digest, returns the existing state and never creates or resets an admission. Reusing `permit_id` with a different digest or payload returns `permit_id_collision`, quarantines the new bytes, and changes neither authoritative record. Policy construction re-evaluates current trust and revocation state before reserve and output commit.

### Local admission lifecycle and eight cross-plane rules

The durable lifecycle is:

```text
available -> reserved(job_id) -> spent
                    |
                    +-> available  (terminal failure before durable output)
```

The eight cross-plane rules are:

1. Reserving an available admission, recording the bound confirmation, and creating the durable job is one `canonical.sqlite3` transaction.
2. Repeating the same confirmed request with the same `job_id` resumes a reserved job or reads a completed job; it never reserves another admission.
3. A process restart after reservation resumes the same job from durable state.
4. Committing the promised durable contribution, marking the admission spent, and linking it to the result is one `canonical.sqlite3` transaction.
5. A terminal failure before any promised durable output releases the reservation; a later retry may reserve that admission again.
6. A job with committed durable output cannot release or reuse the admission because presentation, export, projection, or a later optional operation failed.
7. Existing retained results and existing data remain accessible without another admission; a deleted result returns only its durable deletion outcome.
8. Background work cannot reserve an admission without prior confirmation of the exact capability and the verbatim `credit_cost` value carried by the signed permit.

A retry or resumed attempt while the job remains `reserved(job_id)` uses the same admission and job and does not invoke a second reserve event. A terminal pre-output outcome changes `reserved(job_id)` back to `available` in a canonical transaction that also records the terminal job outcome.

### Durable output and spend atomicity

The promised durable output is one complete versioned structured knowledge contribution conforming to ADR 0014. Its random UUIDv4 `generation_id` is the contribution identity, is allocated with the prepared job, and is stored as immutable `promised_generation_id` before confirmation content is returned.

The committed contribution includes the permit, job, organization, account, capability, parameter, input, contribution-schema, and provenance identities required by ADR 0014. Completeness validation requires all identities and digests to agree, stable finding identifiers to be unique, required normalized values and evidence references to be present, coverage and calibration to validate, and the manifest or equivalent completeness record to recompute. A validation failure occurs before promised output and cannot spend the admission.

Reservation freezes the canonical revision, canonical content digest, and immutable input selection named by the confirmed parameter digest. A later canonical revision does not mutate or invalidate a committed contribution. If the exact input becomes unavailable before output commit, the job records terminal pre-output outcome `input_unavailable` and releases the reservation.

Attempt staging, caches, downloaded artifacts, checkpoints, logs, layouts, graph overlays, search indexes, and aggregates are not promised output. Only the canonical contribution and immutable records reachable from its completeness record are authoritative.

Projection construction or rebuild failure is not a pre-output failure and never releases the admission. Viewing, exporting, backing up, deleting, reprojecting, and rebuilding a committed contribution require no permit state transition. Rebuilding or restoring a projection from an existing contribution is recovery, not a new execution.

Deletion removes contribution content and dependent projections but retains a content-free tombstone with contribution, job, permit, account, schema, digest, and spent-admission identity. Deletion does not make the permit reusable and the kernel does not reconstruct deleted contribution content from source data, projections, caches, exports, or staging.

### Execution fencing and restart recovery

The ADR 0014 job-fencing rules apply. Claiming or resuming a reserved job increments `attempt_fence`, installs a new random UUIDv4 `job_attempt_id` and lease, and succeeds only while the admission remains `reserved(job_id)` and no unexpired attempt owns it.

The output transaction requires the job to remain reserved, the caller to own the current attempt identifier and fence, and no contribution or deletion tombstone to exist for that job. Canonical constraints make `job_id`, `generation_id`, and `permit_id` individually unique in the contribution/tombstone relation. The first transaction satisfying all predicates wins. A stale or concurrent attempt commits nothing and reads the winning job state.

Process termination before output commit leaves the job recoverable under the same reservation. Restart creates a newly fenced attempt for that job; it does not allocate another job, contribution identity, or admission. A terminal pre-output decision releases the permit only through rule 5.

### Spent replay and completed-job retrieval

The permit-consumption decision table governs the `reserve` event only. The `terminal_failure` transition belongs to the lifecycle state machine rather than that decision table. For `reserve`, `permit_state = spent` returns `permit_spent` even when `requested_job_id` equals the job linked to the committed contribution. The immutable `permit-consumption/spent-replay.json` vector has that reading.

Returning a completed job is a separate authenticated job-read path. It loads the durable job by `job_id`, checks the requester's newly built `RuntimePolicy` identity and account scope, and returns the existing contribution without invoking the reservation decision table.

After result deletion, that read path returns semantic outcome `result_deleted` with `job_id`, `generation_id`, and deletion identity but no contribution content, projection pointer, or artifact location. An HTTP representation uses `410 Gone`.

### Job allocation, idempotency, and scope

Before the first prepare request, Bridge creates a cryptographically random UUIDv4 `request_idempotency_key`, persists it across response loss and reconnect, and reuses it for every prepare, confirmation, resume, and read of one intended execution. A distinct intended execution uses a new key even when its parameters are identical. Brain does not deduplicate distinct keys by parameter digest, time window, principal, or account.

The first authenticated prepare stores a durable mapping from the installation-unique `request_idempotency_key` to authenticated principal, creator account, capability, parameter digest, random UUIDv4 `job_id`, and random UUIDv4 `promised_generation_id` in `canonical.sqlite3` before returning confirmation content.

Repeating the key with identical bindings returns the same prepared, confirmed, reserved, completed, failed, or deleted job state. Repeating it with a different principal, account, capability, or parameter digest returns `idempotency_key_reuse` and changes no state.

The confirmation request carries both `request_idempotency_key` and `job_id`. The confirmation-and-reservation transaction verifies their durable mapping, records confirmation once, and reserves at most one permit. A duplicate submission or retry after a lost response returns the existing job state. Neither identifier is authority; the complete current `RuntimePolicy` predicate is evaluated for every state transition.

The job's organization and creator account are immutable and come from independently authorized identity and account scope. Every prepare, confirmation, resume, output commit, and read requires exact equality with that scope.

### Parameter digest

The parameter digest is `sha256:` plus lowercase SHA-256 over the UTF-8 RFC 8785 serialization of exactly `capability`, `creator_account_id`, `canonical_revision`, `canonical_content_digest`, `immutable_input_selection_digest`, `execution_parameters = {}`, `pipeline_revision`, `pipeline_identity_digest`, and `pipeline_config_digest`. Reusing `job_id` with a different digest fails closed with `job_parameter_mismatch` and changes neither job nor admission state.

### Confirmation binding and retention

The confirmation record durably binds:

- the locally authenticated principal and credential or session reference;
- organization, installation, and creator account;
- `request_idempotency_key`, `permit_id`, `job_id`, and `promised_generation_id`;
- capability and parameter digest;
- canonical input revision and immutable input-set reference;
- the exact displayed `credit_cost` value carried opaquely by the signed permit;
- local confirmation time; and
- confirmation-schema version.

Admission rows, prepared-request mappings, confirmation records, job and contribution identity, parameter digest, output link, and content-free deletion tombstones are retained for the installation lifetime. Deleting contribution content does not shorten idempotency retention or make a spent admission reusable. Backups and restores treat these records as authoritative canonical state.

### Authoritative backup-set recovery

Every supported backup of authoritative state has a random `backup_set_id`. Its manifest binds that identifier and installation identifier to the exact SHA-256 digest, schema version, and backup sequence of both `auth.sqlite3` and `canonical.sqlite3`. Backup holds the authorization gate and settles or rolls back staged permit imports before copying either file.

Restore verifies both files against one manifest before enabling capability transitions. Files from different backup sets, a missing authoritative file, or a digest mismatch enter `authoritative_restore_mismatch`; in that state the kernel does not import permits, reconcile staged imports, reserve or release admissions, start or resume jobs, or commit contributions.

Recovery requires a matching authoritative pair from one verified backup set. Restoring an older internally matched pair can restore an earlier admission state; this is an inherent offline replay property of local-only admission and does not depend on hosted observation.

## Consequences

### Positive

- Reservation and job creation are atomic.
- Contribution commitment, spend, and linking are atomic.
- Retries and restarts before output reuse one durable job and reservation.
- Completed contributions remain accessible and projectable without another admission.
- The spent replay vector retains its reserve-event meaning.

### Negative

- Admission, job, confirmation, contribution-link, and tombstone state require installation-lifetime retention.
- A separate authenticated read path is required for completed jobs.
- An older internally matched backup can restore an earlier local admission state.
- Deleting contribution content does not make that execution reproducible under the spent permit.

## Confirmation

### At acceptance

- Contract review verifies the dedicated protected type, audience, subject, closed schema, signing purpose, identifier namespace, installation binding, no validity window, and `execution_limit = 1`.
- State review verifies each of the eight cross-plane rules, the reserve-only `spent-replay` reading, the separate completed-job read path, and installation-lifetime idempotency retention.
- Contribution review verifies canonical output completeness, atomic contribution/spend/link commitment, deletion tombstones, and permit-independent projection recovery.

### At implementation

- Decision-adapter tests use `permit-consumption/reserve-available.json`, `duplicate-reservation.json`, `resume-same-job.json`, `failed-job-release.json`, `spent-replay.json`, and `wrong-installation.json` without changing vector inputs or expected bytes.
- Fault-injection tests interrupt every write in reservation/job creation and contribution/spend/link transactions and assert all-or-nothing recovery.
- Concurrency tests race stale and current attempt fences and assert one winning contribution.
- Request-boundary tests assert stable `job_id` and `promised_generation_id`, exact account and parameter binding, duplicate-response recovery, and changed-binding rejection.
- Projection tests delete or fail `projections.sqlite3` after canonical output commit and assert that the admission remains spent while the contribution rebuilds the projection.
