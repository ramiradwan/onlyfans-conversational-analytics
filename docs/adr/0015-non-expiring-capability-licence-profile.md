# ADR 0015: Define the non-expiring capability licence profile

- Status: accepted

## Context and problem statement

ADR 0014 defines capability objects without assuming that execution consumes authority. A default profile is needed for a capability that may run repeatedly and incrementally over locally available data.

The profile must remain distinct from the single-execution permit in ADR 0016. Its signed object identifies durable capability scope; it is not an execution counter and does not acquire a per-execution lifecycle.

## Decision drivers

- Authorize repeated and incremental local execution.
- Keep the profile cryptographically distinct from every other capability-object type.
- Bind authority to an exact organization, installation key, and seat scope.
- State compatible algorithm artifacts and version-selection rules explicitly.
- Define activation, transfer, replacement-installation, and recovery behavior without a clock dependency.
- Inherit common job safety and contribution provenance from ADR 0014.

## Decision outcome

Choose **a non-expiring `CapabilityLicence` signed-object profile as the default capability profile**.

### Purpose-separated signed object

`CapabilityLicence` is a distinct protected type. Its contract defines a licence-verification audience, a licence subject form, a closed payload schema, and a `capability-licence` signing purpose used by no ADR 0008 grant, no `SingleExecutionPermit`, and no other capability profile. The exact wire identifiers are fixed by the cross-plane contract catalog; the kernel requires exact matches and never aliases them.

The subject is the canonical tuple of object identity, organization, installation, seat, capability or module, and licensed major version. Subject components are encoded in the contract-defined order and form and must equal the corresponding closed-schema fields byte for byte.

> [!NOTE]
> *Canonical* here is the encoding sense defined in ADR 0014, not the storage sense.

The closed schema rejects unknown fields and contains:

- immutable `licence_id`;
- `organization_id`;
- `installation_id`, `installation_key_id`, and installation-key thumbprint;
- `seat_id` and closed `seat_scope`;
- `capability_id` or `module_id` and `licensed_major_version`;
- compatible algorithm-artifact family and version constraints;
- closed update-rights and fallback-version-rights records;
- activation and recovery binding fields; and
- contract-designated opaque display metadata, if present.

The object has no expiry, not-before boundary, or other validity window. Verification does not compare any field with the local clock.

### Identity, installation, and seat scope

The licence identity is the tuple of `licence_id`, capability or module identifier, licensed major version, organization identifier, installation identifier, installation key identifier, installation-key thumbprint, seat identifier, and seat scope.

The runtime/security kernel accepts the object into `RuntimePolicy` only when its organization and installation fields exactly match the independently authorized identity and locally enrolled installation under ADR 0014. It also requires an exact local seat binding within the signed `seat_scope`. A seat identifier does not supply a principal, role, account, or account binding.

The capability or module identifier and licensed major version select only the named capability surface. They do not authorize a different module, a different major version, or a wider account scope.

### Algorithm artifacts, updates, and fallback versions

The compatible algorithm-artifact family identifies the only artifact lineage that may implement the capability. The kernel verifies the selected artifact's family and version against the signed compatibility constraints before execution.

The update-rights record is a closed set of allowed artifact version transitions within the signed family and licensed major version. The fallback-version-rights record is a closed set or range of earlier compatible artifact versions that may be selected when the preferred artifact is unavailable or fails local validation.

Neither right is inferred from semantic version ordering, local availability, a later catalog, or another licence. A version or family not explicitly allowed by the signed object is rejected. Artifact selection and its digest are recorded in job and contribution provenance.

### Repeated and incremental execution

A verified `CapabilityLicence` authorizes repeated execution of its named capability over locally available data while the complete `RuntimePolicy` predicate holds. It also authorizes incremental execution over a later canonical revision when the signed capability and algorithm-artifact constraints support that input.

Ordinary execution under this profile has no `available -> reserved -> spent` transition and no per-execution authorization-state change of any kind. Starting, retrying, completing, or deleting a job does not mutate the licence object, decrement a counter, create a reservation, or change local licence state.

The profile does not reuse the `SingleExecutionPermit` protected type, audience, subject form, schema, signing purpose, identifier namespace, decision table, or admission state. A `SingleExecutionPermit` cannot be interpreted as a licence, and a licence cannot satisfy a single-execution reservation.

Each execution still creates or resumes a durable job. Job identity, account scope, parameter digest, attempt fencing, restart recovery, result provenance, canonical contribution authority, projection recovery, and access to existing data and results are inherited from ADR 0014.

### Activation

Local activation is the durable association of one verified licence-object digest with its exact enrolled installation and seat binding. Activation succeeds only when signature, protected type, purpose, audience, subject, closed schema, organization, installation key, seat scope, capability identity, and algorithm-artifact rules validate through a newly built `RuntimePolicy`.

Import and activation do not execute the capability. Reimporting byte-identical bytes returns the same object reference and activation state. Reusing `licence_id` with different bytes fails closed and does not replace the active reference.

### Transfer

The bytes bound to one installation and seat do not authorize another installation or seat. Transfer requires a separately signed `CapabilityLicence` object whose organization, target installation identifier, target installation key identifier and thumbprint, seat identifier, and seat scope match the target bindings.

The kernel verifies the new object independently against the pinned trust set. It does not derive target authority from the source object, copy an activation flag, or infer transfer from possession of source bytes.

### Replacement installation

A replacement installation has a different installation key and therefore requires a separately signed object bound to the replacement installation identifier, key identifier, and thumbprint. Restored local state cannot rewrite the signed binding or treat the earlier installation key as equivalent.

If both objects are present locally, each contributes authority only for its own exact installation tuple. Local removal of an earlier object does not alter the signed scope of the replacement object.

### Recovery

Recovery may reimport byte-identical signed bytes or restore an internally matched authoritative backup containing the verified object reference, authenticated trust state, installation and seat bindings, jobs, and canonical contributions. Recovery revalidates the object against the pinned trust set and current local bindings before it contributes to `RuntimePolicy`.

Recovery never synthesizes a licence, changes organization, installation, seat, capability, version, artifact-family, update, or fallback fields, or converts an ADR 0016 permit. Jobs and contributions recovered under ADR 0014 remain existing state and require no new execution.

## Consequences

### Positive

- One signed object authorizes repeated and incremental execution within explicit capability and artifact bounds.
- Ordinary execution performs no authorization-state transition.
- Installation, seat, capability, major-version, update, and fallback scope remain closed and inspectable.
- Job safety and canonical provenance remain uniform across capability profiles.

### Negative

- Artifact compatibility, update, and fallback rules require exact local validation.
- Transfer and replacement installation require separately signed objects for their exact bindings.
- Recovery requires a matching trust path, object digest, and local binding set.

## Confirmation

### At acceptance

- Contract review verifies that protected type, audience, subject, closed schema, signing purpose, and identifier namespace are distinct from ADR 0008 grants and ADR 0016 permits.
- Schema review verifies every identity, installation, seat, capability or module, licensed-major-version, artifact-family, update, fallback, activation, transfer, replacement-installation, and recovery field.
- State-model review verifies that ordinary execution, retry, completion, and deletion perform no per-execution licence-state change.

### At implementation

- Verification tests reject a wrong type, purpose, audience, subject, schema field, organization, installation key, seat, capability, major version, artifact family, update transition, or fallback version.
- Execution tests run repeated and incremental jobs under one unchanged licence reference and assert that no reservation, spend, counter, or other per-execution authorization-state write occurs.
- Recovery tests reimport identical bytes and restore matched state without changing the signed scope; transfer and replacement tests require separately signed target-bound objects.
