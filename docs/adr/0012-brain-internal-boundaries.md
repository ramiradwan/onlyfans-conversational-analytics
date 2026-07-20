# ADR 0012: Define explicit internal boundaries within Brain

- Status: proposed

## Context and problem statement

Brain is the local authority for protocol enforcement, durable conversation state, runtime authentication, and analytics. Those responsibilities belong in the creator-controlled data plane, but an undifferentiated backend would mix transport, security policy, canonical data, and derived processing.

The accepted topology uses one Brain process and one application worker. Internal module boundaries must preserve that simple deployment while keeping authority and dependencies reviewable.

## Decision drivers

- Keep conversation data and ordinary runtime operation local.
- Preserve the atomic sequencing, fencing, configuration, command, and projection rules in ADRs 0001–0009.
- Keep HTTP and WebSocket concerns out of domain and persistence logic.
- Prevent local grant verification from becoming hosted identity or commercial authority.
- Make canonical facts independent of NLP, graph, search, and UI projections.
- Retain repository interfaces and one-process post-commit distribution.

## Considered options

### Keep Brain as one undifferentiated service layer

This minimizes module count.

Trade-off: transport handlers, authentication, ingestion, canonical state, and analytics can acquire implicit dependencies and conflicting ownership.

### Split Brain into separately deployed services

This creates process-level isolation.

Trade-off: it conflicts with the accepted single-machine topology and introduces local coordination, ordering, supervision, and recovery costs.

### Keep one process with three explicit internal modules

The process retains one deployment and one authoritative writer while modules communicate through typed application interfaces and repository contracts.

Trade-off: existing mixed modules require incremental refactoring and dependency checks.

## Decision outcome

Choose **one Brain process with a transport/control shell, a runtime/security kernel, and a canonical analytics engine**.

### Module ownership

| Module | Owns | Does not own |
| --- | --- | --- |
| Transport/control shell | FastAPI startup; HTTP and WebSocket adapters; protocol parsing and serialization; application/control use cases; connection, fencing, lease, and presence coordination; immutable Agent-configuration orchestration; command audit and delivery orchestration; fixed-origin enforcement; and adapter dispatch through repository, clock, and event ports | Grant parsing or verification, authentication policy, canonical conversation mutation, enrichment, or analytics calculations |
| Runtime/security kernel | Local identity and authentication, Agent key proof and pairing, challenges, sessions and tickets, signed-grant verification, revocation, account authorization, and pure capability policy | Connection lifecycle, presence, ingestion, configuration orchestration, command orchestration, canonical data, analytics, customer identity, grant or entitlement issuance, hosted onboarding, or payment state |
| Canonical analytics engine | Ingest sequencing, deduplication, checkpoints and reconciliation; canonical chat and message mutation; normalization and source fidelity; enrichment jobs and results; deterministic metrics; query/export/delete behavior; and graph, search, analytics, and Bridge read-model projections | Network transports, connection lifecycle, runtime credentials, grant parsing, local ticket issuance, configuration orchestration, command delivery, or external provisioning authority |

### Dependency rules

- Endpoint and socket adapters call explicit application/control use cases. The transport/control shell coordinates those use cases through repository, clock, event, kernel-authorization, analytics-engine, and Agent-delivery ports; adapters do not contain application policy or direct SQL.
- The runtime/security kernel accepts typed credential and grant inputs and returns authenticated principal, account-authorization, and capability decisions. It authorizes application behavior but does not orchestrate that behavior.
- The transport/control path establishes and verifies connection, lease, and fencing state, then supplies the canonical analytics engine with a typed authenticated account, capability, and fence context.
- The canonical analytics engine enforces source sequencing, deduplication, checkpoint advancement, snapshot/delta reconciliation, and canonical invariants. An accepted ingest operation commits its delivery identity, checkpoint, canonical mutation, durable projection work, and Bridge revision allocation through one `canonical.sqlite3` unit of work.
- The canonical analytics engine does not import endpoint, socket, cookie, ticket, grant-parser, or hosted-provisioning adapters. It receives authorized typed context and input rather than credentials or grants.
- Immutable Agent configuration and command records are controlled by application use cases in the transport/control shell. Their repositories do not become security-kernel services.
- Post-commit notifications carry committed identifiers and revisions. Consumers recover from SQLite state; notifications are not an authority or durability mechanism.
- Shared protocol value objects and narrow application result types may cross module boundaries. Internal storage models and transport objects do not.

### Analytics-to-command boundary

- The canonical analytics engine may emit a typed command proposal through a narrow application port. A proposal contains the intended account, action type, bounded parameters, provenance, and idempotency input; it is not an executable transport message.
- Command orchestration in the transport/control shell validates the proposal, requests an account-and-capability authorization decision from the runtime/security kernel, records the audit state, and dispatches only an authorized command to the currently fenced Agent.
- The canonical analytics engine never sends to Agent and the runtime/security kernel never delivers commands. Agent remains the sole command executor and reports the result through the accepted protocol.

### Persistence ownership

- `auth.sqlite3` belongs to the runtime/security kernel.
- `canonical.sqlite3` contains control-owned configuration and command records and engine-owned ingest and canonical records. Repository boundaries separate ownership. The complete ingest acceptance path remains one engine-owned file-local unit of work; no ingest transaction spans the security kernel or `auth.sqlite3`.
- `projections.sqlite3` belongs to the canonical analytics engine and remains disposable and rebuildable.
- In-memory repositories implement the same contracts for isolated tests. They are not the production authority.

### Local policy inputs

Signed installation, membership, account-binding, and entitlement grants are inputs to the runtime/security kernel. The kernel verifies them against pinned trust and converts them into typed local authorization and capability decisions. Control and analytics use cases consume those decisions and never parse grants themselves. Brain does not authenticate external customers, maintain commercial organization records, issue licenses or grants, bill users, or run hosted onboarding.

An entitlement may gate new capture and newly licensed enrichment or analytics work. It never blocks viewing, exporting, backing up, or deleting existing local data. Identity and ownership of existing data do not disappear when an entitlement expires.

### Why Brain owns these local responsibilities

Brain is the only trusted local boundary shared by Agent and Bridge. Its internal split keeps authorization in the security kernel, application coordination in the control shell, and conversation truth and analytics in the canonical engine while preserving one local process, one canonical writer, and one consumer-facing state authority. This is local application coordination, not hosted commercial authority.

## Consequences

### Positive

- Brain remains one simple local process without becoming one undifferentiated module.
- Security policy and canonical analytics can evolve behind stable application interfaces.
- Hosted provisioning cannot acquire an implicit runtime or conversation-data path.
- Existing-data access remains independent of entitlement validity.

### Negative

- Current transport-manager and service modules contain mixed responsibilities and need staged extraction.
- The transport/control shell is an application layer, not merely a network adapter, so its use cases and adapter ports require explicit organization.
- The security kernel cannot serve as a convenience location for configuration, commands, ingestion, or other application behavior.
- Static dependency checks and module-level contract tests become required.

### Secondary documentation

After acceptance, `AI-instructions.md`, `communication-spec.md`, and the READMEs under `app/` must be corrected to describe these present-tense boundaries and remove obsolete external-broker, hosted-database, unified-socket, and ingestion claims. Those corrections do not change accepted protocol schemas.

## Confirmation

- Architecture tests reject configuration, command, ingestion, connection, presence, or analytics orchestration dependencies in the runtime/security kernel.
- Architecture tests reject imports from the canonical analytics engine into endpoint, transport, authentication-adapter, grant-parser, or hosted-provisioning modules.
- Endpoint tests use application/control interfaces and prove that transport adapters contain no application policy or direct SQL.
- Repository contract tests run against in-memory and SQLite implementations.
- Ingest tests prove one atomic canonical commit for deduplication, checkpoint, canonical facts, projection work, and Bridge revision allocation.
- Command tests prove that analytics proposals cross only the typed proposal port, kernel policy authorizes issuance, control orchestration records and dispatches the command, and Agent remains the executor.
- Configuration and command tests prove that their orchestration and repositories remain outside the security kernel.
- Authorization tests prove that expired entitlement blocks only newly gated work and permits view, export, backup, and delete of existing data.
- Packaging and runtime tests find one Brain process, one application worker, and no required external broker or hosted runtime dependency.
- Privacy tests prove that no Brain module sends conversation content, participant identity, analytics, capture events, commands, or runtime credentials to the external provisioning plane.
