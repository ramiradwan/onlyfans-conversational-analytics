# ADR 0009: Use a local-first production topology and explicit persistence boundary

- Status: accepted

## Context and problem statement

ADR 0008 keeps conversation processing in a creator-controlled local runtime and selects loopback-only Brain exposure by default. The runtime needs a production process topology, stable UI origin, durable persistence boundary, backup model, and update lifecycle that preserve acknowledged ingest and authentication state across restarts.

Agent is independently scheduled as an MV3 extension, while Brain owns authenticated HTTP and WebSocket services and the canonical data boundary. Bridge is a compiled web application served by Brain.

## Decision drivers

- Preserve the local conversation-data boundary.
- Run without a hosted database, broker, or analytics dependency.
- Keep the default listener unreachable from LAN and public networks.
- Provide a stable origin for WebAuthn, cookies, and WebSocket origin checks.
- Keep acknowledged ingest, configuration, commands, authentication state, checkpoints, and view revisions durable.
- Make graph, search, analytics, and Bridge read models rebuildable from canonical data.
- Keep backup, update, recovery, and rollback understandable on a single-user machine.
- Preserve the repository interfaces and protocol-v1 schemas.

## Considered options

### Use hosted databases or brokers from a local Brain

Brain could run locally while storing conversation state or distributing events through hosted services.

Trade-offs: local capture and access would depend on external infrastructure and conversation data would cross the local boundary.

### Split Brain into several local backend processes

HTTP/WebSocket handling, ingestion, analytics, and event distribution could run as separate processes coordinated through a local broker.

Trade-offs: this adds supervision, ordering, recovery, port, and backup failure modes without improving availability on a single machine. Multiple writers also require leadership and fencing.

### Use one local Brain process and serve the compiled Bridge from it

One per-user Brain process owns authoritative writes, serves the compiled SPA, terminates local HTTP and WebSocket requests, and distributes committed changes in process. Agent remains an independently scheduled MV3 extension.

Trade-offs: the profile supports one Brain worker only and requires another decision before introducing a second authoritative process.

### Serve Bridge from a fixed trustworthy localhost origin

Supported browsers can treat a `.localhost` loopback origin as potentially trustworthy for secure-context APIs.

Trade-offs: WebAuthn and secure-cookie behavior must pass the defined browser-host verification for the exact origin. Cookies remain host-scoped rather than port-scoped.

### Provision a local HTTPS certificate

The installer could create a local trust anchor or leaf certificate.

Trade-offs: trust-store mutation, key protection, renewal, browser differences, uninstall cleanup, and recovery create substantial local failure modes without protecting against a compromised local OS.

### Use a signed desktop shell as the primary UI host

A desktop shell could host the compiled SPA and expose the same Brain challenge and session contract.

Trade-offs: this adds a packaged runtime and update surface. It remains a valid fallback if the browser-host requirements cannot be met.

### Put all SQLite data in one file

One database would simplify file-level backup and permit every write to share one transaction.

Trade-offs: authentication material, large canonical data, and disposable projection churn would share corruption, maintenance, and rebuild scope.

### Separate authoritative stores from rebuildable projections

Authentication, canonical runtime data, and disposable projections can use separate SQLite files as long as no correctness rule depends on a cross-file atomic transaction.

Trade-offs: backup and projection activation require explicit coordination.

### Make graph, search, or analytics stores authoritative

A specialized store could be the only durable form of derived or relational state.

Trade-offs: export, deletion, migration, and recovery would depend on that engine. Canonical data would no longer be sufficient to recreate the product state.

### Encrypt SQLite within the application

The application could require database or per-field encryption.

Trade-offs: key backup, recovery, indexing, migration, and support become more complex while offering limited protection after compromise of the authorized OS session or Brain process.

## Decision outcome

Choose **one loopback-only Brain process serving the compiled Bridge, one MV3 Agent, three local SQLite files, and in-process post-commit event distribution**.

### Runtime topology and lifecycle

The installation has three runtime actors:

1. **Brain** is one signed, per-user background process with one application worker. It owns local HTTP/WebSocket termination, runtime authentication, persistence, projection coordination, event distribution, and scheduled maintenance.
2. **Bridge** is the immutable production frontend bundled with the same release and served by Brain from the fixed local origin.
3. **Agent** is the signed MV3 extension. Its IndexedDB outbox remains the durable capture and retry source until Brain acknowledges ingest. Closing Bridge does not stop Agent capture or Brain processing.

The installer registers Brain as a per-user background application. The launcher verifies the installed Brain instance, starts it when needed, and opens the fixed origin. A second launch reuses the running instance.

The hostname and port remain fixed across installation, restart, and update. If Brain cannot acquire the port, it fails closed, leaves the conflicting process alone, preserves the databases, and reports actionable remediation. Correctness does not depend on graceful shutdown; SQLite recovery, durable checkpoints, Agent retry, and Bridge resynchronization cover abrupt termination.

Production packages contain the compiled SPA and production configuration. They contain no development server, source-tree dependency, development credential, selectable development validator, external broker requirement, or default hosted runtime endpoint.

### Local UI origin

The preferred browser origin is `http://bridge.localhost:17871` with WebAuthn RP ID `bridge.localhost`. Scheme, host, port, and RP ID are one release invariant. Alternate localhost names, IP literals, and dynamic ports are different origins and are not accepted.

The browser-host profile is enabled only when the supported-browser matrix confirms all of the following for the exact origin and RP ID:

- `bridge.localhost` resolves exclusively to loopback without DNS or hosts-file configuration.
- The origin is a secure context.
- Platform WebAuthn enrollment and assertion work with user verification, exact origin and RP ID validation, fresh challenges, and replay rejection.
- Brain can set and receive `__Host-bridge_session` with `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, and no `Domain`.
- The cookie is not sent to ordinary `localhost`, IP literals, or other `.localhost` hostnames.
- Exact `Host`, `Origin`, and CSRF validation reject other origins and ports.
- Start, restart, duplicate launch, update, and port-conflict behavior preserve the origin invariant and enrolled credentials.

Cookies are not port-scoped. A hostile local process serving another port on `bridge.localhost` can receive the host-only cookie; this is part of the local-OS-compromise residual risk. Brain still rejects requests from the wrong origin or port.

If any required browser behavior fails, Bridge uses a signed desktop shell with a fixed secure embedded origin. The shell preserves platform-credential verification, Brain-issued challenges, the same cookie and CSRF properties, and purpose-bound WebSocket tickets. It does not replace the contract with a bearer token, local password, or automatic OS-user trust.

### Loopback API exposure

Brain binds only loopback addresses needed for `bridge.localhost`. Wildcard, LAN, container-published, and public bindings fail closed.

The listener serves:

- compiled Bridge documents and static assets;
- `/ws/agent` and `/ws/bridge`;
- `GET /api/v1/agent/config`;
- bounded local provisioning, WebAuthn challenge/assertion, session, CSRF, Agent-pairing, proof, and ticket routes; and
- authenticated local query, export, backup, deletion, health, and diagnostic routes.

There is no second administrative, ingestion, or analytics listener. Hosted grant refresh and provisioning are outbound HTTPS calls; the hosted plane has no inbound route to Brain. CORS and WebSocket origin policies use exact origins rather than wildcards or reflection.

A non-loopback deployment is a separate profile requiring its own TLS, discovery, firewall, origin, WebAuthn, authorization, update, backup, and threat-model decision. The default cannot be changed through a general environment toggle.

### SQLite authority and file layout

Mutable data lives in a platform-native per-user application-data directory, separate from read-only binaries.

| File | Authority and contents | Backup and rebuild status |
| --- | --- | --- |
| `auth.sqlite3` | WebAuthn credentials, Bridge sessions and CSRF state, challenges, Agent pairings, runtime tickets and tombstones, verified grants, trust references, revocation state, and auth migration metadata | Authoritative; required for same-installation authentication recovery |
| `canonical.sqlite3` | Accepted raw ingest, source identities, deduplication records, canonical chats and messages, ingest checkpoints, configuration history, command records and results, durable projection work, and the monotonic Bridge revision allocator | Authoritative; required in every complete backup |
| `projections.sqlite3` | Graph, search, analytics aggregates, Bridge read models, source high-water marks, and active projection generation metadata | Non-authoritative; may be omitted, deleted, or rebuilt |

Presence observations and live connection leases are ephemeral because freshness, not replay, is authoritative. Agent storage is the pre-acknowledgment retry source; after Brain acknowledges ingest, `canonical.sqlite3` is authoritative.

All files use WAL mode, foreign-key enforcement where applicable, and user-only filesystem permissions. Authoritative files use durable commit settings. No correctness rule depends on a transaction spanning files:

- authentication issue and consumption complete in `auth.sqlite3`;
- ingest acceptance, canonical mutation, deduplication, checkpoint advancement, durable projection work, and revision allocation complete in `canonical.sqlite3`; and
- projection writes occur after the canonical transaction commits.

Repository interfaces isolate storage from protocol behavior. SQLite implementations provide production persistence, while in-memory implementations remain available for isolated tests.

Each file has an independent monotonic schema version and checksummed migration ledger. Signed releases carry ordered migrations. Authoritative migrations are forward-only, restart-safe, transactional where supported, and validated before the listener or Agent capture becomes active. Destructive changes use staged copy-and-validate procedures. Incompatible projections are rebuilt.

Brain refuses normal runtime activation when an authoritative schema is newer than the binary, a migration is missing or checksum-mismatched, integrity validation fails, or an authoritative file cannot enter its required journal mode. Compatible recovery and export tools may still provide read-only access.

### Projection rebuild contract

Canonical records are sufficient to rebuild every graph, search index, aggregate, and Bridge read model. Projections never become the only copy of a fact needed for explanation, export, deletion, or recovery.

Each projection records its name, schema/build version, canonical high-water mark, active generation, and build status. Rebuild proceeds as follows:

1. Read a consistent canonical snapshot and record its high-water mark.
2. Build a new generation deterministically without changing the active generation.
3. Validate referential, count, coverage, and projection-specific invariants.
4. Replay durable canonical work until the new generation catches up.
5. Reserve an activation intent and a new Bridge `view_revision` in `canonical.sqlite3`.
6. Activate the validated generation transactionally in `projections.sqlite3`, then complete the canonical activation intent. Startup reconciles interrupted activation idempotently.
7. Send connected Bridges a fresh `state.snapshot`; no delta from the prior generation crosses the boundary.

Ingest can continue during a rebuild because canonical commits do not depend on projections. Bridge receives a truthful rebuilding state and never receives a partially built generation.

### In-process event distribution

The account-scoped transport manager is the production publish/subscribe mechanism inside Brain. No external or local broker is required.

Notifications are not durability. A service commits its authoritative SQLite transaction before publishing an immutable event containing committed identifiers and revisions. Subscribers recover from canonical checkpoints after restart or dropped notification. Ordering is serialized per `creator_account_id`, and slow Bridge delivery recovers through the ADR 0004 revision protocol.

A second backend worker or authoritative process requires a durable database outbox or broker, leader and fencing rules, cross-process ordering, and backup semantics before it can be enabled.

### At-rest protection, export, and backup

At-rest confidentiality relies on platform full-disk encryption, OS credential protection for non-exportable keys, and user-only permissions on the application-data directory. When a reliable OS signal is available, missing full-disk encryption produces a persistent security warning. The application does not create or escrow a database-encryption key.

Portable export emits documented, versioned creator data and excludes session credentials, ticket or challenge values and digests, private keys, CSRF secrets, and internal auth tombstones.

Installation backup uses the SQLite backup mechanism to create a consistent snapshot of `auth.sqlite3`, `canonical.sqlite3`, migration manifests, checksums, and release metadata. Projections are optional. Backups are written only to a user-selected local destination and are never uploaded automatically. Restoring to different hardware can require identity recovery and credential enrollment because private keys remain non-exportable.

### Update and rollback

Every installation uses the same generic signed release. Binaries are replaceable; mutable databases, logs, and projection scratch space remain under the per-user application-data directory.

Updates are staged and signature-verified. Before an authoritative migration, the updater creates and verifies a consistent backup, quiesces Brain, activates binaries, runs migrations, validates integrity and health, and only then resumes work. Failure keeps the listener closed and enters recovery tooling.

Database migrations have no general down path. Binary-only rollback is allowed when the earlier signed version supports the current schemas. Otherwise rollback restores the earlier binaries and the complete pre-upgrade authoritative backup as one recovery action. Projection stores may be discarded and regenerated.

Uninstall removes binaries and startup registration. Local data is retained for explicit backup or reinstall by default and is deleted only after a separate authenticated confirmation.

### Deferred decisions

This ADR does not select:

- detailed signed-grant formats or signer-rotation procedures, which live in a private cross-plane contracts repository;
- hosted provisioning implementation, which lives in a private hosted control-plane repository;
- NLP, embedding, graph, search, or analytics engines and models;
- non-loopback deployment details;
- multi-machine replication or a second authoritative Brain process; or
- data-retention durations and export presentation.

### Protocol compatibility

These decisions do not change the protocol-v1 schemas or fixtures. The role-specific WebSocket operations, configuration request and response, `state.snapshot`, `state.delta`, `state.resync`, and `system.state` cover the required external behavior. SQLite repositories, projections, migrations, backup, and local UI hosting remain internal implementation concerns.

## Consequences

### Positive

- The runtime has no required hosted database, broker, or analytics dependency.
- One Brain writer and file-local transactions make durability and ordering explicit.
- Authentication and canonical data are isolated from disposable projection churn.
- Agent retry, SQLite recovery, durable checkpoints, and Bridge resynchronization cover abrupt lifecycle events.
- Backup, export, update, and rollback have defined ownership.

### Negative

- The browser-host profile depends on supported-browser behavior and must pass its verification matrix.
- The fixed port is exclusive while Brain runs; a conflict prevents startup.
- Three SQLite files require coordinated backup and explicit avoidance of cross-file atomic assumptions.
- Projection rebuilds can temporarily degrade the UI and consume local time and disk space.
- OS-level encryption does not protect an unlocked or compromised local session.
- Some rollbacks require restoring a pre-upgrade backup.

## Confirmation

- Packaging checks find one Brain worker, bundled Bridge assets, no development server or selectable development authentication, and no required external broker or database.
- Browser-host tests cover secure context, WebAuthn, cookie attributes and isolation, exact origin enforcement, restart, duplicate launch, and port conflicts for `http://bridge.localhost:17871`.
- Startup tests reject wildcard or non-loopback listeners, wrong `Host` or `Origin`, cross-port session use, unknown extension origins, and unrelated fixed-port ownership.
- Repository contract tests run against both in-memory and SQLite implementations.
- Restart and termination tests preserve acknowledged ingest, checkpoints, immutable configuration, command records, authentication tombstones, and monotonically advancing Bridge revisions.
- SQLite tests cover WAL, foreign keys, migrations, integrity checks, crash recovery, and the absence of cross-file atomic assumptions.
- Deleting `projections.sqlite3` rebuilds every read model from canonical data and forces a new Bridge snapshot before realtime deltas resume.
- Static and runtime checks confirm that no production path starts an external broker or enables a second server worker.
- Backup and update tests cover consistent snapshots, restore, interrupted migration, integrity failure, compatible binary rollback, and full pre-upgrade restore.
