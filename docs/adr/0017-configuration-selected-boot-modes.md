# ADR 0017: Select packaged boot mode by runtime configuration

- Status: proposed

## Context and problem statement

The signed package contains a local launcher, a bounded provisioning application,
and the configured Brain runtime. An installation without durable runtime
configuration must expose enough local surface to complete provisioning without
loading settings that do not exist. An installation with configuration must
enter the configured runtime and apply its production validation and activation
checks without a fallback that could expose provisioning after configuration has
become invalid.

Boot selection therefore needs one durable input, an import boundary that can be
tested in a fresh interpreter, and a restart boundary between successful
provisioning and configured execution. The fixed local transport identity is
shared by both applications and does not identify which application should run.

## Decision drivers

- Expose only the bounded loopback provisioning surface when runtime
  configuration is absent, as required by ADR 0008.
- Prevent the provisioning import graph from loading configured-runtime settings
  or `app.main`.
- Make malformed or incomplete configured state fail closed instead of falling
  back to provisioning.
- Give one durable filesystem fact sole authority over application selection.
- Enter the configured graph in a fresh interpreter after provisioning.
- Preserve the fixed loopback listener, origin, port, and request-host controls
  established by ADR 0009.
- Keep provisioning data out of command-line arguments and reject ambiguous
  packaged entry forms.
- Make partial configuration publication incapable of authorizing configured
  startup.

## Considered options

### Import one application graph and enable routes conditionally

One application could import configured settings and provisioning routes, then
enable a route set after inspecting configuration.

Trade-offs: the unconfigured path would cross the configured-settings boundary,
the two surfaces would coexist in one import graph, and an error in route gating
could expose provisioning during configured operation.

### Select a mode through an argument, environment variable, host, or port

The launcher or operator could select provisioning or configured operation with
a command-line flag, environment value, alternate `Host`, URL, address, or port.

Trade-offs: multiple authorities could disagree with durable installation state,
and untrusted invocation or request data could select the less privileged boot
surface. Transport identity would also become coupled to lifecycle state.

### Replace the provisioning application in the same interpreter

Successful provisioning could stop accepting provisioning requests and import
the configured application into the provisioning interpreter.

Trade-offs: provisioning modules and state would remain resident, imports made
before configuration publication could retain unconfigured values, and the
configured import boundary would not be independently observable.

### Select by configuration presence and restart into a fresh interpreter

The packaged Brain child can test one per-user configuration pathname before
importing either application, and successful provisioning can terminate that
child with a distinguished result that the launcher handles by starting a new
one.

Trade-offs: configuration publication and launcher supervision become required
parts of the boot contract, and completion includes one controlled interruption.

## Decision outcome

Choose **configuration-file presence as the sole packaged application selector,
with two exclusive import graphs and a fresh-process restart after provisioning**.

### Packaged entry contract

The packaged entry accepts two process forms:

- no argument invokes the launcher; and
- the exact internal argument `--brain` invokes one Brain child.

Every other argument vector is rejected with the fixed usage result. Arguments
carry no provisioning claim, identity, credential, configuration value, or
application-mode selection. The internal `--brain` form selects the Brain child
role, not either Brain application.

The Brain child resolves the platform-standard per-user runtime configuration
pathname and tests only whether that file exists. The data-directory resolution
selects where the per-user file is located; it does not supply a second boot
mode. No configuration field is parsed to decide which graph to import.

Exactly one of two application import graphs is selected:

1. If the runtime configuration file is absent, the child imports and exposes
   only the bounded provisioning application.
2. If the runtime configuration file is present, the child imports the
   configured application through `app.main`.

There is no third application graph and no fallback from the configured graph
to the provisioning graph.

### Import boundary

The configuration-absent branch must not import `app.core.config`, call
`get_settings`, import `app.main`, or import another module that eagerly crosses
that boundary. Provisioning receives only its bounded dependencies. This is an
import-time security property, not only a route-visibility property.

The configuration-present branch may import configured-runtime settings and
`app.main`. Configuration parsing, settings validation, trust and persistence
checks, and runtime activation checks occur inside that branch. A malformed,
incomplete, mismatched, or otherwise unusable file therefore refuses configured
startup or activation according to the applicable production check; its
presence never causes provisioning to be selected.

### Shared transport constraints

Both applications use the same single worker, loopback bind address
`127.0.0.1`, port `17871`, and fixed Bridge origin
`http://bridge.localhost:17871`. Exact `Host` and `Origin` validation remains
part of each exposed surface. The address, origin, port, `Host`, request URL, and
listener state constrain transport and ownership; none is a boot-mode input.

A verified listener can be reused only when its address, executable image, and
user identity match the signed local Brain. An unrelated owner of the fixed
port is left running and causes a fail-closed launch result.

### Configuration publication and restart

The proposed requirement is that successful provisioning publish the complete
runtime configuration to the per-user pathname atomically and durably before it
reports completion. To satisfy that requirement, publication must write and
synchronize a complete candidate before an atomic replacement or creation makes
it visible. A failed write, synchronization, or atomic replacement must leave
either the prior complete file or no file at the selector pathname; it must not
expose a partial selector.

#### Implementation gap and pre-acceptance requirement

This is not current implementation behavior. `app/core/first_run.py` creates
the final selector pathname directly with `O_EXCL` and then writes, flushes, and
synchronizes its contents. An interruption after creation can therefore leave a
present empty or partial selector; the exception cleanup does not cover a
process termination. That implementation does not yet satisfy this proposed
decision.

Before acceptance, publication must write a complete candidate, synchronize it,
atomically install or replace it at the selector pathname, clean up a failed
candidate, and have interruption and fault-injection coverage. Until that gap
is closed, a current failure can select the configured graph and fail closed,
but this decision must not claim the stronger no-partial-selector guarantee.

The durable authentication state referenced by the configuration must also be
available. Configuration publication precedes the durable authorized-account
record. An interruption between those writes can therefore select the
configured graph with no authorized account; it must not grant account access
or return to provisioning implicitly.

After both completion writes succeed, the provisioning application requests
server shutdown. The Brain child returns the distinguished process exit status
`75` only for that completed shutdown. The launcher accepts `75` only from the
provisioning child, verifies that configuration is present, and starts the same
signed Brain command again. The new child observes the file and imports the
configured graph in a fresh interpreter.

The completion status is not a general child-success status and does not alter
the fixed transport identity.

### Failure behavior and recovery boundary

The following is the proposed required behavior after the publication gap is
closed. Until then, the implementation-gap qualification above applies.

| Condition | Required behavior |
| --- | --- |
| Configuration is absent | Select only the bounded provisioning graph. |
| Configuration is present but malformed, incomplete, mismatched, or unusable | Select the configured graph and fail its applicable production validation or activation check; do not enter provisioning. |
| Configuration publication, synchronization, or atomic replacement fails | After the proposed publication mechanism is implemented, do not report provisioning completion or request the distinguished restart. Preserve the prior complete selector state or absence. The current direct-create sequence does not establish this guarantee. |
| Provisioning child exits with a status other than `75` | Report `provisioning_exit_failed`; do not treat the exit as completion. |
| Exit `75` is observed without the configuration file | Report `configuration_unavailable`; do not start configured operation. |
| The configured replacement child cannot start or acquire the listener | Report the applicable start failure or timeout. Preserve durable configuration and authentication state. |
| Port `17871` has an absent, ambiguous, or unverified owner | Start and verify the signed Brain only when the port is free; otherwise refuse launch without terminating the owner. |
| The packaged entry receives an unexpected argument vector | Reject it with the fixed usage result before importing an application graph. |
| Configuration is deleted | A later child mechanically observes absence and selects provisioning, but deletion is not a supported downgrade or recovery protocol. Existing durable state can refuse repeated finalization, and this decision defines no reconstruction, reset, or authorization bypass. |

Recovery of authoritative stores and configuration remains subject to the
explicit backup, restore, update, and rollback boundaries in ADR 0009. This
decision adds no recovery tool and assigns no recovery meaning to configuration
deletion.

### Security rationale

The intended selector is a local durable fact produced by verified provisioning
rather than a caller-controlled request value. Keeping the provisioning graph
free of configured settings avoids premature secret and persistence
dependencies. Keeping a present but invalid file in the configured branch
prevents corruption or tampering from reopening the enrollment surface. The
proposed atomic durable publication would prevent partial bytes from becoming
an ambiguous authority, but the current direct-create implementation does not
yet provide that property. The fresh interpreter ensures that configured
imports are evaluated only after the durable selector is visible and that
provisioning-only in-memory state cannot carry into runtime operation.

The fixed listener and exact request validation reduce the local network attack
surface, but they do not attest provisioning state. Treating them as selectors
would weaken the durable boundary.

## Consequences

### Positive

- Boot selection has one inspectable and testable authority.
- The unconfigured application has a smaller import and route surface.
- Invalid configured state cannot silently reopen provisioning.
- Provisioning-only state is removed by an interpreter boundary before runtime
  imports execute.
- Transport identity remains stable across both graphs and the restart.
- Command-line invocation cannot inject provisioning data or select a Brain
  application.

### Negative

- The proposed decision requires configuration publication, process exit,
  launcher supervision, and fresh startup for successful completion; its
  atomic-publication requirement remains an implementation gap before
  acceptance.
- A durable configuration file with incomplete adjacent state selects the
  configured graph and can leave the installation unavailable until explicit
  recovery succeeds.
- The fixed port is exclusive; an unrelated owner prevents either graph from
  starting.
- Deleting configuration can expose the provisioning graph without producing a
  valid recovery state, so filesystem deletion cannot be offered as a recovery
  instruction.
- Interruption and fault-injection coverage is required at the configuration
  publication and restart boundaries before this decision can be accepted.

## Confirmation

- A fresh-process unconfigured test observes the provisioning application,
  `config_imported=false`, and a successful bounded health response.
- An import-boundary test fails if the absent-file branch imports `app.main`.
- A presence test proves that even an otherwise empty configuration file selects
  `app.main`; separate configured-runtime tests prove malformed and incomplete
  state fails closed.
- Entry-point tests accept only no arguments and the exact internal `--brain`
  form and reject provisioning payload arguments.
- Completion tests must prove configuration publication precedes completion,
  successful completion requests configured restart, exit `75` is
  distinguished, and the launcher starts a fresh configured child.
- Pre-acceptance fault-injection tests must interrupt candidate write,
  synchronization, atomic replacement, completion shutdown, and
  replacement-child startup and verify the failure behavior above.
- Listener tests cover fixed address, origin, port, exact `Host`, verified owner,
  start timeout, and port conflict without treating any transport value as a
  selector.
- Recovery tests prove that malformed configured state cannot select
  provisioning and that deletion selects it only by the absent-file rule,
  without resetting durable state or authorizing downgrade or repeated account
  binding.
