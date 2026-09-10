# Architecture boundaries

`docs/architecture-boundaries.json` is the authoritative machine-readable architecture baseline. This document is the human-readable description of repository architecture, authority boundaries, safety zones, protected invariants, dependency rules, and declared exceptions.

## System components

The repository comprises three runtime subsystems, release packaging, attestation materials, governance manifests, and shared cross-runtime contracts:

- **Brain (`app/`)**: Local Python backend running on the creator's machine. It hosts the local HTTP and WebSocket endpoints, authenticates sessions, coordinates first-run provisioning, commits canonical conversation state to SQLite, and runs derived analytics. There is no standalone `brain/` directory.
- **Agent (`extension/`)**: Chrome Manifest V3 extension responsible for creator-visible observation and durable delivery. It normalizes browser events, manages an IndexedDB outbox, evaluates consent, and delivers validated frames to Brain.
- **Bridge (`frontend/`)**: React and TypeScript single-page application. It consumes Brain-owned state over local WebSockets and authenticated REST endpoints to present real-time dashboards and conversation analytics.
- **Packaging and release (`packaging/`, `tools/packaged-signing-rule/`, `tools/legal-release-bindings/`, `tools/packaging_policy.py`)**: Build-time scripts, packaging manifests, installer definitions, and release verification tools that qualify and seal release artifacts without participating in normal runtime data flow.
- **Attestation materials (`attestation/`, `tools/engineering_attestation.py`)**: Engineering attestation verification keys, public signer definitions, and attestation CLI tooling.
- **Architecture governance (`docs/architecture-boundaries.json`, `docs/architecture-boundaries.md`)**: Machine-readable authority manifest and human architecture documentation governing system boundaries and invariants.
- **Shared contracts (`shared/`, `app/protocol/`, `extension/protocol/`, `frontend/src/protocol/`)**: Protocol v2 schemas, golden wire fixtures, and legal evidence locks implemented across runtimes. External vendored contracts under `contracts/` are snapshot dependencies and excluded from internal architecture modules.

## Authority classifications

Every module in `docs/architecture-boundaries.json` has an assigned authority class:

- **Authoritative**: Subsystems that own primary state of record, security gates, or release qualification. Mutations to authoritative state must be atomic and initiated only through approved ingress interfaces, subject to authoritative deletion and retention lifecycle policies. Examples include canonical persistence (`canonical-persistence`), persistence coordination (`persistence-coordination`), Agent acquisition outbox (`agent-runtime`), security policies (`security-trust`), protocol definitions (`protocol-core`), vault APIs (`brain-vault-api`), authentication APIs (`brain-auth-api`), packaging release tools (`packaging-release`), attestation verification (`attestation-verification`), and governance manifests (`architecture-governance`).
- **Derived**: Subsystems computing rebuildable or secondary state from authoritative sources. These can be regenerated from scratch from canonical history. Examples include analytics pipelines, graph projections, and enrichment analyzers (`analytics-semantic-foundation`, `analytics-analyzers-metrics`, `application-services`).
- **Presentation**: Subsystems formatting and displaying state to the user without owning durable business truth. Examples include Bridge views and components (`bridge-presentation`), client stores (`bridge-orchestration`), and non-authoritative REST presentations (`brain-api-presentation`).
- **Composition**: Factory and bootstrap entrypoints that assemble and connect subsystems from above without implementing business domain logic. Examples include application entrypoints (`brain-runtime-bootstrap`) and temporary composition seams (`persistence-factory`, `persistence-projection-coordination`).

## Change-safety zones

Zones describe the architectural blast radius of file modifications. A safety zone provides informational review context; it does not alone determine mandatory governance ceremony.

| Zone | Blast radius | Scope and modules |
|---|---|---|
| **Green** | Low | Pure presentation components, styling, themes, layout components, visual test harnesses (`bridge-presentation`). Defects are visible immediately in the UI and do not corrupt durable data. |
| **Yellow** | Moderate | Client-side stores, API client adapters, non-authoritative presentation endpoints (`bridge-orchestration`, `brain-api-presentation`). Changes coordinate stable interfaces but must not redefine canonical semantics. |
| **Orange** | High | Semantic foundation, metric calculations, knowledge graph schema, projections, service workflows, and projection coordination (`analytics-semantic-foundation`, `analytics-analyzers-metrics`, `application-services`, `persistence-projection-coordination`). A defect can produce structurally valid but semantically wrong analytics across multiple features. |
| **Red** | Critical | Canonical persistence, persistence coordination, Agent capture and durable delivery, runtime security, provisioning, wire protocol, vault APIs, WebAuthn endpoints, runtime bootstrap, packaging, release tools, and architecture governance (`architecture-governance`, `packaging-release`, `attestation-verification`, `ci-workflows`, `shared-legal-evidence`, `agent-capture`, `protocol-core`, `agent-runtime`, `brain-transport`, `persistence-factory`, `canonical-persistence`, `persistence-coordination`, `security-trust`, `provisioning-surface`, `brain-vault-api`, `brain-auth-api`, `brain-runtime-bootstrap`). Defects risk data loss, authorization bypass, or corrupted distribution. |

## Protected-impact pull-request review

The safety zone is always reported as review context; it does not itself require
architecture evidence. The manifest's `protected_impact.invariant_path_mappings`
contains the narrow semantic paths that require a PR author to disposition a
potentially affected invariant as `affected` or `not affected` with a rationale.
This mapping is deliberately narrower than module ownership: for example, an
Agent icon remains Red context but does not by itself claim a durable-delivery
change.

Changes to an enforced rule definition, an exception-ledger entry, or an
authority/trust declaration are protected governance impact. They require an
architecture rationale, an actual invariant or boundary declaration, and safety
evidence. `tools/check_boundary_declaration.py` compares the checked-out base
and head manifest and parses one exact `## Architecture impact` section from the
pull-request body without calling a network API.

## Classified architectural modules

The following twenty-five modules partition the repository's production namespaces:

| Module ID | Zone | Authority | Responsibility |
|---|---|---|---|
| `architecture-governance` | Red | Authoritative | Machine architecture authority and human-readable architecture contract governing repository boundaries (`docs/architecture-boundaries.json`, `docs/architecture-boundaries.md`). |
| `packaging-release` | Red | Authoritative | Windows packaging scripts, installer definitions, PyInstaller spec, runtime file closure manifests, packaging policy, and release signing verification tools (`packaging/**`, `tools/packaged-signing-rule/**`, `tools/legal-release-bindings/**`, `tools/packaging_policy.py`). |
| `attestation-verification` | Red | Authoritative | Engineering attestation verification keys, public signer definitions, and attestation verification CLI tools (`attestation/**`, `tools/engineering_attestation.py`). |
| `ci-workflows` | Red | Authoritative | Continuous integration and release workflow control plane (`.github/workflows/**`, `.github/pull_request_template.md`, `.github/ISSUE_TEMPLATE/**`). |
| `shared-legal-evidence` | Red | Authoritative | Legal activation evidence schemas, risk disclosure locks, and verified retention acceptance artifacts (`shared/legal/**`, `artifacts/**`). |
| `agent-capture` | Red | Authoritative | Browser-side observation and normalization of creator-visible platform activity under consent control (`extension/capture/**`). |
| `protocol-core` | Red | Authoritative | Cross-runtime protocol v2 schema definitions, golden fixtures, and bidirectional frame validation (`app/protocol/**`, `extension/protocol/**`, `frontend/src/protocol/**`, `shared/fixtures/protocol/**`). |
| `agent-runtime` | Red | Authoritative | Agent durable delivery, WebSocket connection management, outbox persistence, signing/history coordination, preview UI, and MV3 service worker runtime (`extension/**` excluding `capture/` and `protocol/`). |
| `canonical-read-models` | Orange | Authoritative | Neutral canonical read-model type ownership shared by canonical persistence, analytics, and transport without assigning ingestion behavior to the type namespace (`app/canonical/**`). |
| `brain-transport` | Red | Authoritative | Brain network admission, WebSocket connection lifecycle, protocol framing, and session dispatch to canonical persistence (`app/transport/**`, `app/api/endpoints/transport_ws.py`). |
| `persistence-factory` | Red | Composition | Persistence assembly module that constructs and returns persistence-owned repositories only (`app/persistence/factory.py`). |
| `persistence-projection-coordination` | Orange | Composition | Coordination between canonical revisions and projection publication activation (`app/persistence/projection_activation.py`). |
| `canonical-persistence` | Red | Authoritative | Core authoritative canonical conversation storage, SQLite database lifecycle and SQLCipher runtime qualification, schema migrations, and irreversible deletion closure (`app/persistence/history.py`, `app/persistence/database.py`, `app/persistence/migrations/**`, `app/persistence/deletion_operations.py`, `app/persistence/auth.py`, `app/persistence/private_files.py`, `app/persistence/sqlite_api.py`, `app/persistence/sqlcipher_runtime.py`). |
| `persistence-coordination` | Red | Authoritative | Repository aggregation, backup coordination, and retention restore operations bridging persistence with service, transport, and analytics concerns (`app/persistence/repositories.py`, `app/persistence/backup.py`, `app/persistence/retention_restore.py`). |
| `security-trust` | Red | Authoritative | Side authority governing local authentication, device-bound keys, activation gate, grant verification, and capability permits (`app/security/**`, `app/api/security.py`). |
| `provisioning-surface` | Red | Authoritative | Isolated first-run onboarding web surface, claim submission, creator association, and configuration handoff (`app/provisioning/**`). |
| `brain-vault-api` | Red | Authoritative | Creator vault REST endpoints governing hard deletion closure, vault lifecycle, and authenticated consent actions (`app/api/endpoints/creator_vault.py`). |
| `brain-auth-api` | Red | Authoritative | WebAuthn ceremony endpoints and local device authentication flows (`app/api/endpoints/webauthn.py`). |
| `brain-api-presentation` | Yellow | Presentation | HTTP REST endpoints exposing read models, setup routes, and compiled Bridge static asset delivery (`app/api/**`, `app/static/**`, `app/templates/**`). |
| `brain-runtime-bootstrap` | Red | Composition | Top-level application assembly, FastAPI application lifecycle, CLI entrypoints, core shared models/utilities, and bootstrap composition (`app/main.py`, `app/bootstrap.py`, `app/launcher.py`, `app/core/**`). |
| `application-services` | Orange | Derived | Application workflow orchestration for analytics read services, configuration, command execution, and retention maintenance (`app/services/**`). |
| `analytics-semantic-foundation` | Orange | Derived | Derived semantic foundation: canonical-read gateway, graph identity and schema, deterministic pipeline, projection stores, and explicitly configured process-local analytics runtime (`app/analytics/canonical_source.py`, `app/analytics/pipeline.py`, `app/analytics/runtime.py`). |
| `analytics-analyzers-metrics` | Orange | Derived | Derived feature analytics, metrics calculations, and enrichment analyzers consuming canonical read models or graph projections (`app/analytics/analyzers/**`, `app/analytics/metrics/**`). |
| `bridge-presentation` | Green | Presentation | User-facing React presentation components, views, layouts, design system elements, and visual harnesses (`frontend/src/components/**`, `frontend/src/views/**`). |
| `bridge-orchestration` | Yellow | Presentation | Bridge client-side state management, WebSocket session synchronization, authenticated REST API integration, test suites, and build tooling (`frontend/src/store/**`, `frontend/src/services/**`). |

## Non-production policy

Repository paths outside the classified production modules are governed by explicit non-production policies in `docs/architecture-boundaries.json`:

- **External vendored contracts (`contracts/**`)**: Upstream schema and contract snapshots tracked for compatibility verification, isolated from internal architecture modules.
- **Development and test namespaces (`tests/**`, `tools/**`, `docs/**`, `.husky/**`, `.git/**`, `.github/**`, `extension/test-fixtures/**`)**: Test suites, maintenance tools, verification scripts, and documentation harnesses not included in production runtime deliverables.
- **Root metadata files (`README.md`, `LICENSE`, `pyproject.toml`, etc.)**: Repository root configuration and project metadata.

Any untracked or unclassified path that is not in a production module and not covered by an explicit non-production exclusion fails closed during architectural validation.

## Protected architectural invariants

The machine manifest defines twenty-two protected architectural invariants that CI and test harnesses must preserve:

| Invariant ID | Owner | Severity | Assurance | Requirement / Quality scenario |
|---|---|---|---|---|
| `canonical-authority` | Brain canonical persistence | Critical | Documented | HistoryRepository is the sole authoritative commit point for acknowledged platform conversation effects. (ADR 0001, ADR 0009) |
| `checkpoint-monotonicity` | Brain transport and persistence | Critical | Qualified | Source sequence positions advance monotonically per stream; gaps trigger sync resync rather than advancing sequence. (ADR 0004, ADR 0010) |
| `replay-idempotency` | Brain canonical persistence | Critical | Qualified | Replaying previously committed snapshot chunks or delta deliveries produces no duplicate effects and preserves logical identity. (ADR 0004, ADR 0010) |
| `atomic-canonical-commit` | Brain canonical persistence | Critical | Qualified | Snapshot commits transition all chunked events and stream head in a single atomic database transaction. (ADR 0009, ADR 0010) |
| `snapshot-integrity` | Brain canonical persistence and Agent outbox | High | Qualified | Snapshot chunk count, chunk order, and record counts must match the snapshot staging contract before commit is acknowledged, and conflicting entity content or duplicate identifiers are rejected as invariant violations. (ADR 0010, app/persistence/history.py) |
| `deletion-closure` | Brain persistence and lifecycle maintenance | Critical | Qualified | Hard deletion irrevocably clears canonical records, derived projections, graph edges, and prevents resurrection upon replay. (ADR 0009, ADR 0020) |
| `durable-agent-delivery` | Agent transport runtime | Critical | Qualified | Observed platform activity is stored in durable IndexedDB outbox until explicit server ACK is received. (ADR 0001, ADR 0004) |
| `protocol-compatibility` | Protocol schema and validator runtimes | High | Documented | Protocol v2 schemas and shared golden fixtures evaluate equivalently in Python, Agent JS, and Bridge TS validators. (ADR 0006, ADR 0010) |
| `capture-control-separation` | Agent capture architecture | High | Documented | Observation and normalization modules have no direct control over outbox publication, consent, or transport. (ADR 0001, ADR 0022) |
| `transport-analytics-separation` | Brain transport and services | Medium | Documented | Transport admission routes into persistence without directly constructing or invoking analytics pipelines. (Section 3) |
| `persistence-analytics-separation` | Brain persistence and analytics | Medium | Documented | Canonical persistence does not construct analytics-facing adapters or depend on analytics modules. (Section 3, Task 7B) |
| `persistence-transport-separation` | Brain persistence and transport | Medium | Documented | Persistence modules do not import transport or WebSocket connection management. (Section 3) |
| `graph-identity` | Brain analytics and knowledge graph | High | Qualified | Graph entities and participant/message IDs are deterministically derived from canonical IDs without ambiguous collision. (Section 5, app/analytics/identity.py) |
| `derived-state-determinism` | Brain analytics pipeline | High | Qualified | Clean rebuilds of analytics metrics and graph from identical canonical state produce identical semantic outputs within ReproducibilityContext. (Section 6, Task 6A) |
| `graph-semantic-reproducibility` | Brain analytics knowledge graph | High | Qualified | Knowledge graph structure and semantic edges are reproducible across rebuilds within ReproducibilityContext. (Task 6A) |
| `projection-reproducibility` | Brain analytics and projection store | High | Qualified | Incremental projection computation achieves semantic and provenance equivalence with a clean rebuild from canonical head under identical ReproducibilityContext, excluding explicitly documented transient fields. (Task 6B) |
| `projection-publication-revision` | Brain analytics publication | Critical | Qualified | Projections refuse publication if their source revision does not match the authoritative canonical revision; qualified Task 6B active projection and graph material matches canonical head under ReproducibilityContext. (ADR 0020, Section 5) |
| `derived-referential-closure` | Brain analytics and projection store | High | Qualified | Every referenced entity in derived projections resolves to an allowed extant derived entity or canonical entity. (Task 6B) |
| `provenance-integrity` | Brain analytics pipeline | Medium | Qualified | Published analytics generations carry canonical revision, content digests, and pipeline identity witnesses. (ADR 0020, Section 5) |
| `consent-authorization` | Agent consent and Brain security | Critical | Documented | Full capture and telemetry operate only under valid creator consent; revocation immediately ceases capture. (ADR 0021, ADR 0022) |
| `provisioning-trust` | Brain provisioning and security | Critical | Documented | First-run provisioning validates signed claim packages and creator association before local runtime activation. (ADR 0008, ADR 0019) |
| `release-integrity` | Packaging and attestation | Critical | Documented | Release artifacts are bound to immutable source coordinates and requalified before signing and attestation. (packaging policy, attestation specifications) |

## Enforced and documented dependency rules

The repository specifies thirteen architectural boundary rules governing cross-module dependencies:

| Rule ID | Type | Enforcement | Source modules | Target modules | Description |
|---|---|---|---|---|---|
| `rule-canonical-read-model-ownership` | Protected | Enforced | `analytics-semantic-foundation`, `persistence-coordination`, `persistence-projection-coordination`, `brain-transport` | `canonical-read-models` | AccountReadModel is defined only in app.canonical.read_models; analytics, persistence coordination, and transport import it directly from that owner and must not import or re-export it through app.transport.ingestion. |
| `rule-canonical-persistence-no-upward` | Forbidden | Enforced | `canonical-persistence` | `analytics-semantic-foundation`, `analytics-analyzers-metrics`, `application-services`, `brain-api-presentation`, `provisioning-surface`, `brain-transport` | Core canonical persistence modules (history, database, migrations, deletion_operations) must not import feature, API, provisioning, transport, or analytics orchestration modules. |
| `rule-canonical-history-gateway` | Protected | Enforced | `analytics-analyzers-metrics` | `canonical-persistence` | Only approved gateway modules may depend directly on app.persistence.history; ordinary analytics must use canonical read source. |
| `rule-agent-capture-isolation` | Forbidden | Enforced | `agent-capture` | `agent-runtime` | Agent capture modules must produce observations only and not import transport, outbox publication, or command execution. |
| `rule-persistence-factory-no-analytics` | Forbidden | Enforced | `persistence-factory` | `analytics-semantic-foundation` | Persistence factory must not construct or depend on analytics-facing adapters. |
| `rule-projection-coordination-boundary` | Boundary | Documented | `persistence-projection-coordination` | `analytics-semantic-foundation` | Projection activation coordination between canonical revisions and analytics projection publication is an entangled composition seam recorded under current design. |
| `rule-no-service-to-transport` | Forbidden | Enforced | `application-services` | `brain-transport` | app.services.insights_service must not directly import or discover app.transport; bootstrap injects the canonical read source into app.analytics.runtime, and canonical read types have their own owner. |
| `rule-transport-analytics-separation` | Forbidden | Enforced | `brain-transport` | `analytics-semantic-foundation` | Transport layer must not construct analytics-facing adapters or directly invoke analytics pipelines. |
| `rule-bridge-no-canonical-writes` | Forbidden | Documented | `bridge-presentation`, `bridge-orchestration` | `canonical-persistence` | Bridge frontend components and stores consume Brain state and must not act as an ingestion or canonical write proxy. |
| `rule-runtime-policy-confinement` | Protected | Enforced | `brain-api-presentation`, `application-services` | `security-trust` | Runtime policy and role authorization decisions are confined to the security kernel. |
| `rule-grant-licence-admission-confinement` | Forbidden | Enforced | `security-trust` | `provisioning-surface` | Grant and licence authorization modules must not resolve or reference capability permit admission markers. |
| `rule-agent-protected-acyclic` | Forbidden | Enforced | `agent-runtime`, `protocol-core` | `agent-runtime`, `protocol-core` | Protected Agent protocol, transport, and runtime kernel modules must remain acyclic; phase-one structural protection does not prove correct dependency direction. |
| `rule-bridge-protected-acyclic` | Forbidden | Enforced | `bridge-orchestration`, `protocol-core` | `bridge-orchestration`, `protocol-core` | Protected Bridge protocol, store, and service kernel modules must remain acyclic; phase-one structural protection does not prove correct dependency direction. |

## Normal feature development lane

Ordinary product features follow a standard unidirectional path:

1. Consume existing canonical data through the read boundary (`canonical_source.py`) or existing semantic models.
2. Implement feature-specific computations or metrics in derived analytics.
3. Expose the result through a feature-specific read model.
4. Deliver the model through an authenticated API endpoint or WebSocket channel.
5. Render the data in Bridge presentation components.

Developers should not modify canonical schemas, transport logic, or security policies for routine analytics or UI features.

## Composition and bootstrap responsibilities

Composition must occur above the components being composed:

- Persistence modules must not construct analytics-facing adapters.
- Transport modules must not construct analytics-facing adapters.
- Analytics services must not discover transport infrastructure.
- Explicit bootstrap code (`app/main.py` or dedicated composition wiring) registers the canonical read source, analytics storage configuration, and projection activation with the analytics runtime.

## Paths requiring escalation

Changes that touch any of the following require explicit architectural rationale and safety evidence:

- Modifying a protected architectural invariant.
- Adding, altering, or removing an enforced dependency rule.
- Altering trust, authorization, or provisioning models.
- Introducing, updating, or expiring an architecture exception.
- Introducing a new production namespace. Unknown production paths fail closed.

## Declared architectural exceptions

The repository acknowledges one current-design composition seam tracked in `docs/architecture-boundaries.json`:

| Source | Target | Rule | Status | Expires | Tracking Issue |
|---|---|---|---|---|---|
| `app/persistence/projection_activation.py` | `app.analytics` | `rule-projection-coordination-boundary` | `current_design` | None | `DESIGN-PROJECTION-ACTIVATION-IDENTITY` |

## Enforcement mechanisms

Architectural rules are enforced by static checkers and automated test suites:

- **Manifest validation**: `tools/validate_architecture_boundaries.py` and `tests/test_architecture_boundaries.py` verify manifest schema, zone validity, exception expiration, executable control references, and production path classification.
- **Fail-closed path classification**: Every production file must match a declared module pattern. Unclassified production paths cause immediate validation failure.
- **Dependency boundaries**: Import Linter module-granularity contracts and executable tests verify forbidden import directions and approved gateway access.
- **Permanent negative controls**: Isolated import fixtures, invalid manifest fixtures, and source-level regression assertions prove that boundary checks reject violations.
- **Documentation consistency**: Automated checks ensure that `docs/architecture-boundaries.md` and `docs/architecture-boundaries.json` remain synchronized.

## Semantic assurance lifecycle

Semantic invariants progress through an auditable lifecycle:

- **`documented`**: Requirement or scenario reference is documented. Evidence and falsifiers may be planned.
- **`qualified`**: Automated tests execute property checks against production-equivalent runtimes, and a permanent oracle falsifier proves detection capability.

Under the progressive qualification rule:
- Task 1 (PR 1) establishes the architecture contract with invariants in the `documented` state.
- Tasks 5 and 6 advance implemented ingestion and rebuild invariants to `qualified`.
- Task 9 requires all claimed invariants to be fully `qualified`.

## Pre-implementation import census (Task 2)

Prior to activating module-level dependency contracts, a pre-implementation import census was performed for the four protected canonical persistence modules and all direct importers of `app.persistence.history`.

### Protected persistence core imports

| Module | Imported module/symbol | Classification | Rationale |
|---|---|---|---|
| `app.persistence.history` | `sqlite_api` | Implementation | Database driver abstraction. |
| `app.persistence.history` | `app.persistence.database` (`PROJECTION_KEY_SCOPE`, `CanonicalSQLite`, `LocalSQLite`, `ProjectionsSQLite`) | Contract/value type and composition/runtime | Connection handle classes and key scope constant within persistence. |
| `app.persistence.history` | `app.persistence.migrations` (`MigrationChecksumError`, `MigrationRunner`) | Contract/value type and implementation | Migration execution and checksum validation within persistence. |
| `app.persistence.history` | `app.persistence.projection_pipeline` (`CanonicalProjectionConversation`, `CanonicalProjectionMessage`, `DeterministicProjectionPipeline`, `ProjectionPipeline`) | Contract/value type and implementation | Projection data structures and pipeline runner within persistence. |
| `app.persistence.database` | `sqlite_api` | Implementation | Database driver abstraction. |
| `app.persistence.database` | `app.persistence.private_files` (`PrivateFileSecurityError`, `apply_private_file_security`, `reject_path_aliases`) | Contract/value type and implementation | File permissions and alias validation within persistence. |
| `app.persistence.database` | `app.security.local_data_key` (`LocalDataKeyError`, `database_key`, `protect_local_secret`) | Contract/value type and implementation | Device-bound database key derivation from security kernel. |
| `app.persistence.migrations` | `sqlite_api` | Implementation | Database driver abstraction. |
| `app.persistence.migrations` | `app.persistence.database` (`LocalSQLite`) | Implementation | Connection handle within persistence. |
| `app.persistence.migrations` | `app.persistence.managed_recovery` (`prune_managed_recovery_files`) | Implementation | Migration recovery cleanup within persistence. |
| `app.persistence.migrations` | `app.persistence.private_files` (`PrivateFileSecurityError`, `apply_private_file_security`, `sync_directory`, `sync_file`) | Contract/value type and implementation | File synchronization within persistence. |
| `app.persistence.deletion_operations` | `app.persistence.database` (`CanonicalSQLite`) | Implementation | Connection handle within persistence. |

None of the four protected modules import `app.analytics`, `app.services`, `app.api`, `app.provisioning`, or `app.transport`. Module-level fencing for Contract A requires no refactoring of neutral types.

### Direct importers of app.persistence.history

| Importing module | Imported symbols | Classification | Status under Contract B |
|---|---|---|---|
| `app.analytics.canonical_source` | `HistoryRepository` | Implementation | Approved gateway module. Wraps canonical history into the read-only `CanonicalReadModelSource` interface. |
| `app.analytics.rebuild` | `HistoryRepository` | Composition/runtime | Approved gateway/integration module. Iterates canonical history to rebuild projections. |
| `app.transport.manager` | `IngestResult`, `InvariantViolation`, `StreamKey` | Contract/value type | Transport ingestion dispatch admitting Agent frames to `HistoryRepository`. Outside analytics. |
| `app.api.endpoints.history` | `ProjectionCursorStale` | Contract/value type | Exception type handling stale cursor responses. Outside analytics. |
| `app.api.endpoints.transport_ws` | `InvariantViolation` | Contract/value type | Exception type handling WebSocket error frames. Outside analytics. |
| `app.persistence.factory` | `HistoryRepository`, `ProjectionRepository` | Composition/runtime | Persistence-owned repository assembly. It returns canonical resources only; bootstrap composes the analytics read adapter. |
| `app.persistence.projection_activation` | `HistoryRepository` | Composition/runtime | Projection publication coordination seam. Outside protected core; tracked under current design exception. |

Ordinary analytics modules (metrics, analyzers, feature modules, and graph projections) are excluded from importing `app.persistence.history` directly and must consume canonical data through `app.analytics.canonical_source`.
