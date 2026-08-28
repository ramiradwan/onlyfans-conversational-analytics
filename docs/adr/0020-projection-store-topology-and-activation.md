# ADR 0020: Separate the Bridge read model from the analytics projection store

- Status: accepted
- Supersedes: the three-file count in the ADR 0009 decision-outcome headline, the `projections.sqlite3` authority-table row in ADR 0009, and the projection-ownership sentence in ADR 0010, for the placement of derived output only

## Context and problem statement

ADR 0009 records three local SQLite files and assigns graph, search, analytics aggregates, and Bridge read models to one of them. ADR 0010 restates that assignment and places analytics, NLP, and LPG output in the same file.

The runtime declares four database paths (`app/core/config.py:34-40`), and first-run configuration writes all four into the runtime configuration file (`app/core/first_run.py:110-115`). The fourth file, `analytics-projections.sqlite3`, is not a second copy of the third. It has its own migration catalog (`app/analytics/database.py:77-84`), its own schema identity (`app/analytics/sql/0004_topic_property_pair.sql:28-35`), its own key scope (`app/analytics/database.py:75`), its own readers, and an activation model that is not the two-slot model ADR 0010 specifies.

No accepted decision states what the fourth file owns, which store answers a given read, or how its visible generation is resolved. ADR 0019 names all four files but decides encryption only.

## Decision drivers

- Describe the persistence topology the runtime creates rather than an earlier intended one.
- Leave canonical authority and the ADR 0009 rebuild contract unchanged.
- Resolve the activation semantics of the fourth store, so that adding a database does not leave visibility undefined.
- Keep every statement in ADR 0009 and ADR 0010 that this correction does not require in force.

## Considered options

### Treat the fourth file as an implementation detail of the third

The authority table could keep one projection row and leave the analytics store undocumented.

Trade-offs: the two stores have different schemas, activation models, readers, and failure behavior. One row cannot state which store answers a query, which activation ledger governs it, or what deleting either file costs.

### Merge the two projection stores into one file

The Bridge read model and the analytics projection could share one file and one transaction.

Trade-offs: each store carries a separately versioned migration catalog, so a shared file fails migration-checksum validation on open (`app/core/config.py:37-39`), and the analytics store factory rejects a shared path outright (`app/analytics/factory.py:66-70`). The Bridge read model advances incrementally per canonical revision into an alternating slot, while the analytics store builds immutable whole generations; one file would impose one lifecycle on both.

### Record four stores and state each projection store's activation model

Each file receives an authority row, and each projection store states how a reader resolves the visible generation.

Trade-offs: two rebuildable stores mean two build paths, two quarantine paths, and two canonical activation ledgers to reconcile at startup.

## Decision outcome

Choose **four local SQLite files, with two independent rebuildable projection stores, each carrying its own activation model and its own canonical witness ledger**.

### File layout and authority

Mutable data continues to live in the platform-native per-user application-data directory.

| File | Authority and contents | Backup and rebuild status | Schema and wiring |
| --- | --- | --- | --- |
| `auth.sqlite3` | Local authentication and local provisioning state: WebAuthn enrollments (`app/persistence/auth_sql/0001_local_authentication.sql:27-37`), Bridge sessions and their CSRF digest (`:61-79`), challenges (`:81-122`), Agent pairings (`:39-59`), runtime tickets (`:124-159`), the `consumed_at`/`invalidated_at` tombstone columns that retire a spent challenge or ticket in place and drop it from the live-lookup indexes (`:103-104`, `:139-140`, `:174-179`), revocation state and bindings (`:1-8`, `:161-172`), verified grant references (`:10-25`), the installation key reference (`app/persistence/auth_sql/0002_installation_key_reference.sql:1-18`), the authorization epoch (`app/persistence/auth_sql/0003_authorization_epoch.sql:1-4`), provisioning candidates (`app/persistence/auth_sql/0006_provisioning_state.sql:1-11`), authorized account bindings (`app/persistence/auth_sql/0007_authorized_account_bindings.sql:1-22`), claim submissions (`app/persistence/auth_sql/0008_claim_submissions.sql:1-16`), the onboarding progress outbox (`app/persistence/auth_sql/0009_onboarding_progress_outbox.sql:1-31`), and this file's `schema_migrations` ledger of applied version, name, checksum, and timestamp, written by the shared migration runner rather than by any catalog file (`app/persistence/migrations.py:212-222`, `:309-311`) | Authoritative; required for same-installation authentication recovery | `app/persistence/auth.py:609-618`; `app/persistence/auth_sql/` |
| `canonical.sqlite3` | Accepted raw ingest (`app/persistence/sql/0002_history_acquisition.sql:296-312`), source stream identities and ingest checkpoints (`:1-19`), stream epochs (`:29-37`), deduplication evidence: per-event fingerprints under a unique source sequence (`:304`, `:307-308`), per-entity content hashes (`:226`, `:244`), and a material-conflict unique index (`:284-294`), snapshot staging and committed-snapshot records (`:39-64`, `:66-85`, `:87-120`, `:122-156`, `:193-217`, `:314-328`), account chats and messages (`:219-253`), tombstones (`:259-269`), conflicts (`:271-282`), coverage state (`:330-364`), durable projection work (`:366-375`), the Bridge revision allocator (`:21-27`), launcher handoffs (`:398-403`, `app/persistence/sql/0006_launcher_session_handoff.sql:1-7`), configuration history (`app/persistence/sql/0001_canonical_plane.sql:1-51`), command records and results (`:53-100`), and both projection activation ledgers (`app/persistence/sql/0002_history_acquisition.sql:380-393`, `app/persistence/sql/0003_analytics_projection_activation.sql:1-30`, `app/persistence/sql/0005_analytics_projection_writer_capability.sql:9-20`) | Authoritative; required in every complete backup | `app/persistence/factory.py:66-67`; `app/persistence/sql/` |
| `projections.sqlite3` | The protocol-v2 Bridge read model: slot-scoped account generations, conversation summaries, message pages, per-message analysis, per-account analytics documents, LPG nodes and edges, the Bridge change log, and applied projection work | Non-authoritative; may be omitted, deleted, or rebuilt | `app/persistence/history.py:1803-1824`; `app/persistence/projection_sql/0001_read_models.sql:1-135` |
| `analytics-projections.sqlite3` | The analytics projection: store identity, publication epochs, per-account generation lifecycle, generation-scoped analytics documents, graph nodes and edges, partition statistics, and graph algorithm results | Non-authoritative; may be omitted, deleted, or rebuilt | `app/analytics/database.py:62-84`; `app/analytics/sql/0003_opaque_graph_contract.sql:37-337` |

Both projection stores hold derived output; neither is the only copy of a fact needed for explanation, export, deletion, or recovery. The analytics store keys its rows by opaque account and node references rather than platform identifiers (`app/analytics/sql/0003_opaque_graph_contract.sql:74-78`, `:217-220`).

Runtime wiring is split. `canonical.sqlite3` and `projections.sqlite3` are opened once at import (`app/transport/manager.py:1417-1432`). The analytics store is opened lazily on first analytics use, and only under the SQLite canonical backend (`app/services/insights_service.py:111-123`); its readers are the `/api/v1/insights` routes (`app/api/endpoints/insights.py:32-33`).

### Bridge read model activation

`projections.sqlite3` uses the two-slot model of ADR 0010. Every read-model row carries `projection_slot`, constrained to 0 or 1 (`app/persistence/projection_sql/0001_read_models.sql:3`), and a build writes into the slot the active generation does not occupy (`app/persistence/history.py:2583-2589`). Visibility is resolved from canonical state: `account_heads.view_revision` (`app/persistence/sql/0002_history_acquisition.sql:21-27`) selects one activated row in `projection_activation_intents` (`app/persistence/sql/0002_history_acquisition.sql:380-393`), and a read returns rows only when the projection row matches that pointer exactly (`app/persistence/history.py:1838-1893`). The canonical intent converges after the projection transaction commits (`app/persistence/history.py:2031-2040`).

### Analytics projection activation

**The analytics projection does not use the two-slot model.** Its schema has no slot column. Visibility is resolved per account by generation status, under a scheduler-owned publication epoch, and is witnessed in `canonical.sqlite3`.

- Generations are identified by opaque `generation_id` and move monotonically through `building`, `validated`, `activation_pending`, `active`, and `retired` (`app/analytics/sql/0003_opaque_graph_contract.sql:72-171`). A partial unique index admits at most one `active` generation per account (`app/analytics/sql/0003_opaque_graph_contract.sql:173-174`). The number of stored generations is not fixed at two: a configured count of retired generations is retained for rollback and the remainder is deleted in bounded batches (`app/analytics/sqlite_projection_store.py:855-879`, `app/analytics/factory.py:42-43`).
- A publication epoch fences one scheduler's writes. Registration writes the canonical epoch first and mirrors it into the analytics store second; if the mirror fails, the canonical epoch is revoked in compensation, and publication requires the local mirror row to still be `open`, so an unmirrored epoch cannot publish (`app/analytics/sqlite_projection_store.py:404-441`, `:515-525`). Revocation is canonical-first for a separate reason: fencing canonical authority before the disposable store keeps a revoked epoch from becoming eligible for restart activation (`:443-455`). Epoch rows are append-only, transition only from `open` to `revoked`, and cannot be deleted (`app/persistence/sql/0005_analytics_projection_writer_capability.sql:22-41`).
- Publication reserves a canonical activation intent, moves the local generation to `activation_pending`, completes the canonical intent, and only then activates locally (`app/analytics/sqlite_projection_store.py:482-601`).
- Activation retires the previously active generation and promotes the pending one in a single analytics-store transaction, guarded by a compare-and-set against the expected previous generation and revision (`app/analytics/sqlite_projection_store.py:1034-1129`). No slot is reused.
- Every read starts from the local `active` row (`app/analytics/sqlite_projection_store.py:1294-1302`) and then applies the canonical witness as an additional requirement: a matching **completed** witness must still exist and canonical identity must be unchanged, or the store reports no active generation (`:1146-1180`). The witness is a veto over local state, not a source of truth; a completed witness alone never makes a generation readable.

### Why the two orderings differ

The two stores complete their canonical and local writes in opposite orders. One rule produces both: **the write that gates visibility goes last.**

For the Bridge read model the gate is canonical. A read resolves `account_heads.view_revision` to the one activated intent carrying that revision (`app/persistence/history.py:1838-1867`) and returns only the projection generation that pointer names (`:1870-1893`). The projection transaction therefore commits first and the canonical intent converges after it; until it does, the prior generation stays visible.

For the analytics projection the gate is local. A read cannot report a generation active unless a local `status='active'` row exists, and the canonical witness only vetoes such a row. So the local write goes last, and the canonical witness must already be `completed` when it lands. The class invariant states this directly — the store exposes only generations backed by an exact completed canonical witness (`app/analytics/sqlite_projection_store.py:74`) — and the Bridge ordering cannot satisfy it, because a crash between a local activation commit and canonical completion would leave exactly an active row with no completed witness.

Startup reconciliation closes the window the inverted ordering does open. Opening the store runs it unconditionally (`app/analytics/sqlite_projection_store.py:112-113`, `:673-853`); it trusts no local `active` row on its own, retires any whose canonical witness is missing, uncompleted, or not an exact match, and cancels the orphaned reservation (`:686-718`). In the runtime the store is wrapped and constructed on first analytics use (`app/services/insights_service.py:111-123`, `app/analytics/resilient_projection_store.py:326-338`), and a read taken before the store is open fails closed rather than reading the file directly (`app/analytics/resilient_projection_store.py:255-257`). No read reaches an unreconciled store.

Step 6 of the ADR 0009 projection rebuild contract is therefore not superseded. It states the ordering for a store whose visibility gate is the canonical Bridge `view_revision` its own step 5 reserves, and it continues to govern that store. A store whose gate is local satisfies the same rule by inverting the order, so it falls outside that step rather than contradicting it.

### The two canonical activation ledgers

`canonical.sqlite3` holds both ledgers, and they are separate tables with separate semantics:

- `projection_activation_intents` for the Bridge read model, keyed by account and target canonical revision, with states `pending`, `activated`, and `superseded` (`app/persistence/sql/0002_history_acquisition.sql:380-393`).
- `analytics_projection_activation_intents`, `analytics_projection_witness_sequences`, and `analytics_projection_publication_epochs` for the analytics projection, with per-account monotonic witness sequences, at most one reserved intent per account, and terminal state transitions enforced by trigger (`app/persistence/sql/0003_analytics_projection_activation.sql:1-30`, `app/persistence/sql/0005_analytics_projection_writer_capability.sql:9-20`, `:106-114`).

The analytics activation repository is constructed over the canonical database and commits wholly inside it (`app/persistence/factory.py:86`, `app/persistence/projection_activation.py:389-396`). Build and publication ownership is fenced by process identity evidence — owner id, pid, process start time, instance nonce, and a digest of a per-process secret — recorded on both the local generation and the canonical intent (`app/analytics/ownership.py:17-60`, `app/persistence/sql/0005_analytics_projection_writer_capability.sql:56-76`).

### Deleting a store

- Deleting `auth.sqlite3` loses local authentication state; recovery requires re-enrollment on the same installation.
- Deleting `canonical.sqlite3` loses every fact not still held in Agent storage; only a complete backup restores it.
- Deleting `projections.sqlite3` rebuilds every Bridge read model from canonical records and forces a fresh `state.snapshot`. An unreadable or schema-drifted file is quarantined and recreated empty rather than failing startup (`app/persistence/history.py:1815-1824`).
- Deleting `analytics-projections.sqlite3` loses every built generation and cached graph result. Startup cancels any canonical intent whose generation is no longer present (`app/analytics/sqlite_projection_store.py:845-849`), analytics reads report no active generation until a rebuild publishes one, and the new generation receives a fresh witness sequence; canonical witness sequences never decrease. An unreadable file is quarantined the same way (`app/analytics/resilient_projection_store.py:340-358`).

### Scope of this decision

This ADR supersedes three statements, and only for the question of which database holds which derived output and how each projection store resolves its visible generation:

- the "three local SQLite files" count in the ADR 0009 decision-outcome headline, which becomes four. The rest of that headline is unchanged: one loopback-only Brain process serving the compiled Bridge, one MV3 Agent, and in-process post-commit event distribution;
- the `projections.sqlite3` row of the SQLite authority table in ADR 0009, replaced by the four rows above; and
- the sentence in ADR 0010 assigning analytics, NLP, and LPG output to `projections.sqlite3`, narrowed so that the Bridge read model's analytics and LPG rows remain in `projections.sqlite3` while the analytics projection and its graph live in `analytics-projections.sqlite3`.

The `auth.sqlite3` and `canonical.sqlite3` rows above restate their ADR 0009 contents with the tables the runtime creates. Neither row's authority classification changes, and no statement in ADR 0009 that depends on either classification is affected.

Everything else in both ADRs remains in force. ADR 0009 continues to govern process topology, the local UI origin, loopback exposure, canonical and authentication authority, cross-file transaction rules, migration and integrity refusal, the projection rebuild contract, in-process event distribution, export, backup, update, and rollback. ADR 0010 continues to govern acquisition ownership, account isolation, canonical identity, deterministic merge, atomic page ingestion, coverage evidence, bounded snapshot repair, canonical persistence, message paging, and bounded Bridge state.

The rebuild contract's activation step is included in what remains in force. It is stated for a store whose visibility gate is the canonical Bridge revision; the analytics store's gate is local, which places its inverted ordering outside that step rather than in conflict with it.

ADR 0010's two-slot rule is read here as scoped to the store its own paragraph names, `projections.sqlite3`, and it continues to govern that store unchanged. The analytics projection is a store ADR 0010 does not describe, so the activation model stated above is added rather than substituted; it does not weaken the two-slot rule anywhere the two-slot rule applies.

This decision places derived output. It does not rule on whether any derived result is authoritative, and it does not change the ADR 0009 rule that canonical records are sufficient to rebuild both projection stores. A later decision may narrow the same ADR 0010 sentence again on the separate question of which results are authoritative.

Encryption of all four files is decided by ADR 0019 and is not restated here.

## Consequences

### Positive

- The authority table matches the files the runtime creates, so backup, deletion, and recovery reasoning covers every store.
- Each projection store states how a reader resolves its visible generation, so neither store's visibility is inferred from the other's.
- Keeping the stores separate lets the analytics generation lifecycle change without a migration to the Bridge read model, and the reverse.
- The analytics projection cannot serve a generation that canonical state does not witness as completed.
- One rule covers both activation models — the write that gates visibility goes last — so a further store's ordering follows from where its visibility gate lives rather than from precedent.

### Negative

- Four SQLite files need coordinated backup and continued avoidance of cross-file atomic assumptions.
- Two activation models must both be understood to reason about what a reader sees.
- The two stores complete their canonical and local writes in opposite orders. A reader who knows only one store's ordering will infer the wrong one for the other unless the gate rule is carried with it.
- Two rebuildable stores can be stale independently, and readiness reporting must distinguish them.

## Confirmation

- Configuration tests find four distinct database paths and reject a configuration that points two stores at one file.
- Startup tests confirm the analytics store opens under its own migration catalog and refuses a file carrying the Bridge read-model schema.
- Schema tests confirm that no analytics-store table carries a slot column and that at most one generation per account is `active`.
- Activation tests confirm that a generation with no completed canonical witness is never readable, that a revoked publication epoch prevents activation, and that restart after each publication step converges without a third generation becoming visible.
- The alternative ordering is excluded adversarially rather than merely unused. `test_active_before_canonical_completion_is_retired_and_reservation_cancelled` (`tests/test_sqlite_projection_store.py:691-746`) fabricates it directly — a local `active` row whose canonical intent is still only reserved — and asserts that reopening the store retires the generation, cancels the reservation, and leaves no active generation to read.
- Deletion tests delete each projection file independently and confirm that reads report an unavailable projection, that canonical records rebuild it, and that canonical witness sequences do not decrease.
- Backup tests confirm that a restored canonical file and a restored analytics store agree on completed witnesses before either becomes readable.

## Related

- [ADR 0009: Use a local-first runtime and persistence boundary](0009-local-first-topology-and-persistence.md)
- [ADR 0010: Use bounded account-scoped history acquisition](0010-signer-history-acquisition-and-bounded-state.md)
- [ADR 0012: Define internal Brain boundaries](0012-brain-internal-boundaries.md)
- [ADR 0013: Define conversational-analytics scope](0013-conversational-analytics-scope.md)
- [ADR 0019: Encrypt local Brain persistence with device-bound keys](0019-encrypted-local-persistence.md)
