# ADR 0014: Define the local authorization foundation

- Status: accepted

## Context and problem statement

ADR 0008 defines signed grants and local authentication state but does not name the immutable value that combines verified signed objects with local durable state for authorization. Capability authority must compose with that identity and account authority without becoming a second identity system and without assuming that every execution consumes a unit of authority.

This decision defines the common local authorization foundation. ADR 0015 defines the non-expiring capability licence profile, and ADR 0016 defines the single-execution permit profile.

ADR 0009 separates canonical state from rebuildable projections. ADR 0010 assigns analytics, NLP, and LPG output to `projections.sqlite3`. Capability output needs a representation that remains authoritative without making a graph, report, or index canonical.

> [!NOTE]
> In this document, **canonical data** is data that cannot be recomputed from anything else — losing it loses the fact. Anything derivable from it is a **projection**, which can be deleted and rebuilt. **Canonical form** (canonical JSON, a canonical tuple) is unrelated: it is the single specified byte encoding, and says nothing about storage.

## Decision drivers

- Make one immutable value the sole input to every local authorization decision.
- Keep identity and account authority separate from capability authority.
- Bind capability authority to one organization and one enrolled installation without a clock dependency.
- Support capability profiles with different execution semantics.
- Keep execution and its observability entirely local.
- Preserve access to existing data and committed results independently of capability state.
- Make long-running work restartable, fenced, and attributable.
- Preserve pinned trust acquisition while specifying the pin representation.
- Keep contract wire fields distinct from local policy meaning.

## Decision outcome

Choose **an immutable `RuntimePolicy` as the sole input to local authorization, typed and purpose-separated capability objects, local-only execution, and canonical versioned structured knowledge contributions**.

### RuntimePolicy

`RuntimePolicy` is an immutable value produced by the runtime/security kernel from:

- signed objects whose signatures, protected headers, types, purposes, audiences, subjects, and closed schemas have been verified against the pinned trust set; and
- authoritative local durable state, including trust, revocation, principal, credential, installation, organization, account binding, capability-object reference, job, and profile-specific admission facts.

Every local authorization decision accepts exactly one `RuntimePolicy`. Endpoints, transport adapters, job runners, algorithms, and projection workers do not parse signed objects, inspect authorization rows, or combine partial policy inputs to decide authority independently.

`RuntimePolicy.identity` holds the fields represented by `app.api.security.AuthContext`. `AuthContext` is the identity subset of `RuntimePolicy`, not a parallel authorization type, and is insufficient by itself for an authorization decision. This is the refactor anchor for callers that accept `AuthContext` directly.

A `RuntimePolicy` is a decision snapshot rather than durable authority. Its inputs remain authoritative in their owning stores, and the kernel rebuilds it for a request or resumed execution that needs authorization.

Every change to an authorization input increments an `AuthorizationEpoch` in the same transaction as that change. `RuntimePolicy` captures the epoch and signed-object digests from which it was built. A state transition rejects a policy whose epoch is no longer current.

The runtime/security kernel holds one installation-scoped interprocess authorization gate across the final authoritative read, policy construction, and each profile-defined state-transition transaction. Writers that change an authorization input or a profile admission state hold the same gate. The gate is a serialization fence, not a transaction spanning SQLite files.

### Separation of authority

Identity and account authority and capability authority use distinct protected types, audiences, subject forms, closed schemas, and signing purposes. Verification dispatches by protected type and signing purpose before parsing the corresponding closed schema; a valid signature for one purpose cannot validate an object for another.

A capability object supplies no principal, credential, role, account membership, or account binding. It never substitutes for an ADR 0008 grant and never widens the identity, organization, or account scope already established independently. The requested account must be authorized by `RuntimePolicy.identity`, and a resulting job is bound to that exact account.

When an operation requires an ADR 0008 signed-object type in addition to a capability object, the kernel verifies that object independently against the pinned trust set and includes the result as a separate `RuntimePolicy` input. No capability field is interpreted as satisfying that requirement.

### Capability-object category and profiles

A capability object is a verified signed object that contributes typed capability authority to `RuntimePolicy`. Every capability profile defines all of the following:

- a protected type used only by that profile;
- a dedicated audience, subject form, closed schema, and signing purpose;
- an immutable object identifier and capability or module identifier;
- exact organization and installation binding;
- its algorithm-artifact compatibility rules;
- its authorization semantics, including whether local durable state changes when execution begins or completes; and
- its confirmation and recovery rules.

The foundation has two profiles:

- ADR 0015 `CapabilityLicence`, which authorizes repeated and incremental execution without a per-execution authorization-state transition; and
- ADR 0016 `SingleExecutionPermit`, which authorizes one execution through an `available -> reserved -> spent` lifecycle.

The profiles do not reuse a protected type, audience, subject form, schema, signing purpose, or object-identifier namespace. Adding another profile requires another purpose-separated signed-object definition and an explicit execution-state model; this foundation does not infer consumption semantics.

### Installation, organization, and time binding

Every capability object contains an organization identifier, installation identifier, installation key identifier, and installation-key thumbprint. The kernel requires byte-exact organization equality across the object, the independently verified signed objects used by the decision, `RuntimePolicy.identity`, and local organization binding. It also requires exact equality of the installation identifier, installation key identifier, and thumbprint with the locally enrolled installation.

Aliases, case folding, identifier normalization, inherited scope, and partial matches are forbidden. A capability object cannot select an account.

Capability objects have no expiry, not-before boundary, or other validity window. An issuance field may be retained as signed provenance, but verification never compares it with the local clock. Authenticated trust transitions and explicit revocation remain applicable without introducing a clock dependency.

### Local execution and non-observability

Capability algorithms execute on the local machine. No local input, output, graph content, progress, execution count, or usage measurement is reported to the hosted plane.

The hosted plane therefore cannot observe local capability execution. No authorization, retry, recovery, correctness, synchronization, or profile design may depend on it observing an execution or receiving an execution acknowledgment.

### Existing data and committed results

Access to existing local data and already-produced results is independent of current capability state. Viewing, exporting, backing up, deleting, reprojecting, and rebuilding existing data or results are never gated by a capability object, a capability profile state, or permission to start a new execution. This extends the existing-data rule in ADR 0008 and the capability-access rule in ADR 0013.

Deletion does not create authority to execute again. A profile may retain a content-free tombstone or other minimal integrity record needed to preserve job and admission invariants.

### Long-running execution identity, fencing, recovery, and provenance

Every long-running capability execution has a durable, installation-unique random UUIDv4 `job_id`, an immutable organization and account scope, a capability identity, a parameter digest, an input identity, and a promised contribution identity. The job records the capability-object digest and profile that authorized its start.

Each job has a monotonically increasing `attempt_fence`, a nullable random UUIDv4 `job_attempt_id`, and a lease. Claiming or resuming work increments the fence and installs a new attempt identifier in one canonical transaction. Every attempt-sensitive write compares both values. A stale or concurrent attempt commits nothing and reads the winning durable state.

Process termination never allocates a replacement job for the same intended execution. Restart recovery loads the durable job, reauthorizes through a newly built `RuntimePolicy`, claims a new fenced attempt, and resumes or returns the existing outcome according to the profile.

Every committed contribution records the job, capability object and profile, organization and account, input revision and coverage, parameter digest, algorithm artifacts, model or rules identity, policy identity, and attempt fence that produced it. Provenance is immutable with the contribution.

### Canonical structured knowledge contributions

A capability produces a versioned structured knowledge contribution. Each contribution carries:

- contribution schema and version, contribution identity, capability identity, and authorizing profile;
- input revision, immutable input identity, and input coverage;
- stable finding identifiers and normalized values;
- source and evidence references;
- confidence or calibration information;
- model, rules, algorithm-artifact, and policy provenance;
- an as-of identity; and
- supersession links or an explicit statement that it supersedes nothing.

Canonical source data, canonical contributions, and user confirmations and corrections are sufficient to rebuild every report, search index, graph overlay, analytics view, and Bridge read model. A graph representation is never the only copy of a contribution.

This decision supersedes the storage assignment in ADR 0010:56 for capability contributions: contributions are canonical, while analytics, graph, search, and Bridge output remain rebuildable projections. It does not make those projection representations canonical.

An authoritative contribution is a closed-schema record in the canonical contribution store with `authority = capability_contribution`, a non-null contribution identifier, capability identity, job link, immutable provenance, and a validated manifest or equivalent completeness record. Presentation rows, layouts, aggregates, graph nodes and edges, indexes, caches, checkpoints, staging artifacts, and logs lack that authority marker and canonical link and are disposable.

Canonical contributions acquire backup, migration, export, deletion, and retention obligations. Deletion removes contribution content and dependent projections while preserving only profile-required integrity metadata. Retention applies to the contribution and its provenance rather than to any particular projection.

A projection construction or rebuild failure occurs after a contribution has been committed. It is not a pre-output failure and never releases an admission. Rebuilding or restoring a projection from an existing contribution is recovery, not a new capability execution.

Rebuilding or restoring an existing contribution under the same immutable contribution identity, content digest, and provenance is recovery, not a new capability execution. Producing different contribution content, identity, or provenance is not recovery.

### Trust acquisition and pin representation

Brain is provisioned with a pinned signing trust set as required by ADR 0008. Rotation occurs only through a software update or a key-transition statement authenticated by a currently pinned key. Fetching an untrusted key set is insufficient.

Pin representation deviation: Brain pins the canonical trust-set artifact by cryptographic content digest. Key identifiers select keys only inside that already authenticated content. A key identifier, location, package version, or fetched document is not a trust anchor. A content change requires one of the ADR 0008 rotation mechanisms and records the newly authenticated digest in `auth.sqlite3`.

A capability object remains subject to the authenticated trust path and current revocation state whenever policy is built. Import time and a prior successful verification do not override a later authenticated trust transition or explicit revocation.

### Capability delivery boundary and local import

Capability delivery has no normative wire operation. The local path assumed by this decision is an explicit, user-selected import of signed object bytes through the authenticated loopback provisioning surface.

The kernel verifies the bytes against the pinned trust set, validates the profile and exact local bindings, and stores the signed-object digest and parsed reference. Import does not itself authorize execution or create identity or account authority. Byte-identical reimport is idempotent; an object-identifier collision with different bytes fails closed. Defining an interoperable receive operation requires a separate cross-plane contract.

### Contract vocabulary

The kernel interprets only the contract fields needed for local verification and authorization: protected type and algorithm, signer key identifier, signature, issuer, audience, subject, object identifier, profile and schema identifier, organization and installation binding, capability identity, and the profile-specific fields named by ADR 0015 or ADR 0016.

Fields that a profile designates as opaque display metadata, including `credit_cost` and `catalog_version` in the single-execution profile, are retained with the signed object and displayed verbatim when confirmation requires them. The kernel attaches no identity, account, capability, execution-count, or local authorization meaning to those values.

## Consequences

### Positive

- One immutable value is the sole input to local authorization decisions.
- Purpose-separated capability objects cannot create or widen identity and account authority.
- Repeated-execution and single-execution profiles share verification and job-safety rules without sharing state semantics.
- Execution remains local and unobservable to the hosted plane.
- Canonical contributions preserve knowledge independently of disposable projections.
- Signature verification has no wall-clock dependency.

### Negative

- The runtime/security kernel composes policy snapshots from state with different persistence owners.
- Authorization writers and profile transitions share an installation-scoped serialization fence.
- Canonical contributions add schema, storage, backup, migration, export, deletion, and retention responsibilities.
- The product exposes no interoperable capability-delivery operation.

## Confirmation

### At acceptance

- Review the `RuntimePolicy` input boundary, protected-type and signing-purpose separation, exact organization and installation binding, absence of validity windows, and profile cross-references against the signed-object contract catalog.
- Review the local execution boundary and verify that no design requires hosted observation of input, output, graph content, progress, execution count, or usage measurement.
- Review canonical contribution schemas and rebuild definitions to verify that source data, contributions, confirmations, and corrections reproduce every projection and that no graph representation is the only copy.
- Review backup, migration, export, deletion, retention, fencing, restart, provenance, import, trust-transition, and pin-digest rules for closed and fail-closed behavior.

### At implementation

- `tests/test_architecture_admission.py` mechanically asserts that the reserved admission surface is absent from production code: it fails when a production module contains a match from its fixed reserved identifier or string-literal sets. The guard is an adoption gate, not a permanent architectural rule. It remains closed until ADR 0014, ADR 0015, and ADR 0016 are accepted as a set. That acceptance lifts the gate and requires its removal or narrowing as part of the implementation-time transition; the guard is never removed or narrowed before that acceptance. The focused invocation is:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_architecture_admission.py -q
  ```
