<!-- CODE-VERIFY: app/protocol/payloads.py app/protocol/common.py app/transport/ingestion.py app/transport/manager.py app/persistence/history.py extension/transport/durable-outbox.mjs extension/transport/entity-merge.mjs extension/transport/agent-websocket.mjs -->

# Ingestion state contract

This document defines the research-gate contract for Task 5 (PR 5R) of the architectural hardening plan. It is derived from the executable specification in `app/protocol/payloads.py`, `app/protocol/common.py`, `app/transport/ingestion.py`, `app/transport/manager.py`, `app/persistence/history.py`, `extension/transport/durable-outbox.mjs`, `extension/transport/entity-merge.mjs`, `extension/transport/agent-websocket.mjs`, the shared protocol fixtures under `shared/fixtures/protocol/v2/`, and accepted architecture decision records (ADR 0001, ADR 0002, ADR 0003, ADR 0004, ADR 0005, ADR 0006, ADR 0008, ADR 0009, ADR 0010, ADR 0019, ADR 0020, ADR 0021, and ADR 0022). Proposed ADR 0012 is not promoted or assumed.

The contract establishes the state transition catalogue, quality scenarios, independent model scopes, per-transition oracle assertions, persistent backend qualification requirements, CI benchmark calibrations, and gate blocker criteria that govern implementation in Task 5A (independent reference model and state machine), Task 5C / PR 5B (persistent Brain qualification), and Task 5B / PR 5C (real JavaScript Agent harness).

## 1. Operating context and authority boundaries

The production ingestion pipeline spans two distinct runtime components separated by loopback WebSocket transport:

1. **Agent (`extension/`):** Sole producer of raw observations under creator consent. It maintains account-partitioned durable state (`INGESTION_STORES` in IndexedDB), detects semantic no-ops, sequences outbound changes monotonically (`last_source_seq`), stages bounded snapshot chunks, and tracks transport acknowledgement (`acknowledged_source_seq`).
2. **Brain (`app/`):** Authoritative local service. Admission and fencing occur in `app/transport/manager.py`. Authoritative canonical persistence commits strictly through `HistoryRepository` in `app/persistence/history.py`. Read-only canonical state is exposed to downstream analytics through `HistoryAnalyticsSource` in `app/analytics/canonical_source.py`.

In-memory sequencing in `app/transport/ingestion.py` provides a copy-on-write model used in selected unit tests. The authoritative commit point for all acknowledged source effects is `HistoryRepository`.

## 2. Ingestion state transition catalogue

Every entry in this catalogue is classified under one of four normative categories:
- **`normative behavior`:** Invariant required by protocol, persistence authority, or accepted ADRs.
- **`implementation detail`:** Internal execution mechanism subject to refactoring without altering semantics.
- **`existing regression evidence`:** Invariant verified by existing deterministic tests in the repository.
- **`measurement/calibration input`:** Empirical property used for test distribution tuning or CI budget profiling.

Each entry specifies: stimulus, precondition, expected disposition, expected state mutation, expected non-mutation, retryability, persistent/restart expectation, and protected invariant.

### 2.1 Stream admission and session fencing

#### Entry S01: Valid Agent handshake and session establishment
- **Classification:** `normative behavior`
- **Stimulus:** `agent.hello` payload with valid `auth_ticket`, `agent_installation_id`, `requested_creator_account_id`, `capabilities`, `agent_stream_id`, and `last_acknowledged_source_seq`.
- **Precondition:** Runtime activated; account authorized in `SQLiteAuthenticationStore`; valid grant present.
- **Expected disposition:** `accepted` (emits `agent.session`).
- **Expected state mutation:** Allocates transient `AgentLease` in `InMemoryTransportManager` with randomly generated `fencing_token` (`f"fence-{secrets.token_urlsafe(24)}"`), replaces any prior active lease for the creator account, and binds the socket to `(creator_account_id, connection_id)`. Inspects persistent checkpoints and pending snapshots to determine `resume_action` (`"resume"` or `"snapshot_required"`).
- **Expected non-mutation:** Canonical entities, committed events, checkpoints, and persistent stream registrations in `ingest_streams` or `stream_epochs` are not created or mutated by handshake alone (`HistoryRepository._ensure_stream` is not called during handshake).
- **Retryability:** `False` (handshake succeeds; subsequent frames proceed under the issued lease).
- **Persistent/restart expectation:** Lease is in-memory and transient; persistent checkpoints survive restarts.
- **Protected invariant:** Single active Agent lease per account; distinct random fencing token isolates socket ownership without monotonic counter assumptions.

#### Entry S02: Stale session fencing token on ingest frame
- **Classification:** `existing regression evidence`
- **Stimulus:** `ingest.delta` or `ingest.snapshot` presenting a fencing token that does not match the active `AgentLease.fencing_token`.
- **Precondition:** Live WebSocket connection where lease was superseded or token mismatched.
- **Expected disposition:** `rejected` with `code="stale_fence"`.
- **Expected state mutation:** Rejection frame `ingest.rejected` emitted with `code="stale_fence"` and `retryable=False` (`_invalid_ingest` defaults `retryable=False` and the `stale_fence` call does not override it); WebSocket connection receive loop remains open (handler returns `True` to transport loop), but a fresh handshake (`agent.hello`) and newly issued active lease are required to resume ingestion. Close code `4001` (`LEASE_EXPIRED_CLOSE_CODE`) is not invoked here (reserved for lease heartbeat expiration sweeper).
- **Expected non-mutation:** No mutation to `ingest_checkpoints`, `raw_ingest_events`, staging tables, canonical entities, or transport socket lifecycle.
- **Retryability:** `False` (for the rejected frame under the stale fence; Agent must re-handshake to acquire a new active lease and valid fencing token).
- **Persistent/restart expectation:** Persistent storage remains unchanged.
- **Protected invariant:** Single-writer fencing enforcement rejecting outdated tokens without unnecessary transport socket disruption.

#### Entry S03: Same-stream reconnect and lease replacement
- **Classification:** `normative behavior`
- **Stimulus:** `agent.hello` presenting an existing `StreamKey` (`agent_installation_id`, `agent_stream_id`) following process restart or connection drop.
- **Precondition:** Stream identity was previously used; connection dropped or superseded.
- **Expected disposition:** `accepted` (emits `agent.session` with `resume_action="resume"` if checkpoint exists (`checkpoint is not None`), no pending snapshot (`pending_snapshot is None`), and `last_acknowledged_source_seq <= checkpoint`; otherwise `"snapshot_required"`).
- **Expected state mutation:** Allocates a fresh transient `AgentLease` with a new random `fencing_token`, replacing any stale lease in `active_agents`. Persistent `stream_epochs` is NOT incremented upon reconnect; `HistoryRepository._ensure_stream` reuses the existing `stream_epoch` for an existing `StreamKey` when subsequent ingest transactions arrive.
- **Expected non-mutation:** Checkpoints, canonical revision, and existing stream epoch remain unchanged; `stream_epoch` is never incremented merely by reconnecting.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Stream resume checkpoint reflects the exact persistent value in `ingest_checkpoints`.
- **Protected invariant:** Lineage preservation across reconnects; epoch stability for established stream keys; epoch allocation occurs only upon first ingest transaction of a newly registered stream identity (`MAX(stream_epoch) + 1`).

### 2.2 Contiguous canonical deltas

#### Entry D01: Chat upsert with full record
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` where `change.type == "chat.upsert"`, `record_kind == "full"`, `source_seq == checkpoint + 1`.
- **Precondition:** Stream checkpoint established; `chat_id` not tombstoned in `entity_tombstones`.
- **Expected disposition:** `accepted`.
- **Expected state mutation:** Row inserted or updated in `account_chats` (`is_deleted=0`, updated `content_hash`, `winning_stream_epoch`, `winning_source_seq`, `upstream_updated_at`); `raw_ingest_events` appended; `stream_chat_membership` recorded; `ingest_checkpoints.checkpoint` advanced to `source_seq`; `account_heads.canonical_revision` incremented; `projection_work` queued (`work_kind='entity'`).
- **Expected non-mutation:** Unrelated chats and messages untouched; tombstoned entities remain tombstoned.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Changes committed within SQLite transaction; durable across restart.
- **Protected invariant:** Contiguous sequence progression; canonical entity convergence.

#### Entry D02: Chat upsert upgrading placeholder to full
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` where `change.type == "chat.upsert"`, incoming `record_kind == "full"`, existing record has `record_kind == "placeholder"`.
- **Precondition:** Placeholder exists in `account_chats` with matching `chat_id` and `is_deleted=0`.
- **Expected disposition:** `accepted`.
- **Expected state mutation:** `account_chats` record upgraded to `full` with populated `platform_user_id` and `upstream_updated_at`; checkpoint advanced; canonical revision incremented.
- **Expected non-mutation:** Chat ID and child messages preserved.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Upgraded record survives reopen.
- **Protected invariant:** Placeholder promotion monotonic lattice.

#### Entry D03: Chat upsert redundant placeholder ignored over full
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` where `change.type == "chat.upsert"`, incoming `record_kind == "placeholder"`, existing record has `record_kind == "full"`.
- **Precondition:** Full chat already committed in `account_chats`.
- **Expected disposition:** `accepted` (at sequence level).
- **Expected state mutation:** `raw_ingest_events` appended; checkpoint advanced to `source_seq`.
- **Expected non-mutation:** `account_chats` row NOT overwritten; remains `record_kind == "full"`; canonical revision does NOT increment (`changed == False`).
- **Retryability:** `False`.
- **Persistent/restart expectation:** Full record retained in persistent database.
- **Protected invariant:** Informational monotonicity; placeholders cannot downgrade full records.

#### Entry D04: Chat upsert upstream timestamp progression
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` full chat with `upstream_updated_at > existing.upstream_updated_at`.
- **Precondition:** Existing full chat in `account_chats`; identical `platform_user_id`.
- **Expected disposition:** `accepted`.
- **Expected state mutation:** `account_chats` updated with new content and timestamp; canonical revision incremented; checkpoint advanced.
- **Expected non-mutation:** Platform identity immutable.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Latest update persists.
- **Protected invariant:** Last-write-wins by upstream timestamp for mutable chat metadata.

#### Entry D05: Chat upsert stale upstream timestamp ignored
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` full chat with `upstream_updated_at < existing.upstream_updated_at`.
- **Precondition:** Existing full chat in `account_chats`.
- **Expected disposition:** `accepted` (sequence consumed).
- **Expected state mutation:** `raw_ingest_events` appended; checkpoint advanced.
- **Expected non-mutation:** `account_chats` row untouched; canonical revision unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Newer timestamp retained.
- **Protected invariant:** Stale observations cannot overwrite newer canonical chat metadata.

#### Entry D06: Message upsert with valid parent chat
- **Classification:** `existing regression evidence`
- **Stimulus:** `ingest.delta` where `change.type == "message.upsert"`, `source_seq == checkpoint + 1`.
- **Precondition:** Parent `chat_id` exists in `account_chats` with `is_deleted=0`; `message_id` not in `entity_tombstones`.
- **Expected disposition:** `accepted`.
- **Expected state mutation:** Row inserted in `account_messages` (`is_deleted=0`, `content_hash`, text, timestamps); `raw_ingest_events` appended; `stream_message_membership` recorded; checkpoint advanced; canonical revision incremented; `projection_work` queued.
- **Expected non-mutation:** No modification of other messages or parent chat identity.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Message durably stored and queryable via `HistoryAnalyticsSource`.
- **Protected invariant:** Referential closure (parent chat mandatory); append-only message history.

#### Entry D07: Chat delete cascading tombstone
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` where `change.type == "chat.delete"`, `source_seq == checkpoint + 1`.
- **Precondition:** Valid stream checkpoint.
- **Expected disposition:** `accepted`.
- **Expected state mutation:** Row inserted in `entity_tombstones` (`entity_kind='chat'`); `account_chats.is_deleted=1`; all messages under `chat_id` in `account_messages` set to `is_deleted=1`; memberships deleted from `stream_chat_membership` and `stream_message_membership`; checkpoint advanced; canonical revision incremented.
- **Expected non-mutation:** Physical rows retained for audit/tombstone tracking; tombstone is permanent.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Tombstone and deleted flags persist across restart.
- **Protected invariant:** Deletion barrier; complete referential cascade of deletion.

#### Entry D08: Message delete individual tombstone
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` where `change.type == "message.delete"`, `source_seq == checkpoint + 1`.
- **Precondition:** If message exists canonically, `chat_id` matches canonical parent; parent chat exists.
- **Expected disposition:** `accepted`.
- **Expected state mutation:** Row inserted in `entity_tombstones` (`entity_kind='message'`); `account_messages.is_deleted=1` for `message_id`; stream membership deleted; checkpoint advanced; canonical revision incremented.
- **Expected non-mutation:** Sibling messages and parent chat remain active (`is_deleted=0`).
- **Retryability:** `False`.
- **Persistent/restart expectation:** Tombstone persists.
- **Protected invariant:** Fine-grained deletion closure without parent destruction.

#### Entry D09: Coverage observed evidence
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` where `change.type == "coverage.observed"`.
- **Precondition:** Valid stream checkpoint; evidence conforms to `CoverageEvidence` union.
- **Expected disposition:** `accepted`.
- **Expected state mutation:** `coverage_generations`, `coverage_members`, or `account_coverage_heads` updated according to evidence subtype (`generation.started`, `inventory.member`, etc.); checkpoint advanced; canonical revision incremented if coverage state mutated.
- **Expected non-mutation:** Chat and message content tables unmutated.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Coverage heads and member sets persist.
- **Protected invariant:** Historical acquisition coverage tracking; distinct provenance tracking.

### 2.3 Delta anomalies, invariants, and rejections

#### Entry A01: Exact duplicate delta replay
- **Classification:** `existing regression evidence`
- **Stimulus:** Retransmission of `ingest.delta` with identical `event_id`, `source_seq`, and payload content.
- **Precondition:** Event was previously accepted and committed in `raw_ingest_events`.
- **Expected disposition:** `duplicate`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Checkpoint, canonical revision, and entity tables remain completely unchanged.
- **Retryability:** `False` (idempotent success; already committed).
- **Persistent/restart expectation:** Replay after restart yields identical `duplicate` disposition.
- **Protected invariant:** Ingestion idempotency.

#### Entry A02: Event ID reuse with conflicting content
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` presenting an `event_id` already stored in `raw_ingest_events`, but with differing `source_seq` or differing `change` payload.
- **Precondition:** `event_id` exists in `raw_ingest_events`.
- **Expected disposition:** `rejected` with code `invariant_failed`.
- **Expected state mutation:** None in canonical tables; error logged or conflict recorded.
- **Expected non-mutation:** Checkpoint and canonical entities untouched.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Database state unaltered.
- **Protected invariant:** Event identifier immutability.

#### Entry A03: Sequence number reuse with differing event ID
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` with `source_seq <= checkpoint` where `source_seq` was committed for a different `event_id`.
- **Precondition:** `source_seq` exists in `raw_ingest_events` under a different `event_id`.
- **Expected disposition:** `rejected` with code `invariant_failed`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Checkpoint unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** No mutation.
- **Protected invariant:** Source sequence uniqueness per stream.

#### Entry A04: Old sequence replay behind checkpoint
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` with `source_seq <= checkpoint` where `event_id` matches prior commit or was covered by an admitted snapshot.
- **Precondition:** Checkpoint > `source_seq`.
- **Expected disposition:** `duplicate`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Checkpoint and state unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Checkpoint remains at high-water mark.
- **Protected invariant:** Monotonic checkpoint non-regression.

#### Entry A05: Forward sequence gap
- **Classification:** `existing regression evidence`
- **Stimulus:** `ingest.delta` where `source_seq > checkpoint + 1`.
- **Precondition:** Current checkpoint is `N`, incoming sequence is `N + K` (K >= 2).
- **Expected disposition:** `gap` with code `sequence_gap`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Checkpoint stays at `N`; event not written to `raw_ingest_events`; no entity mutation.
- **Retryability:** `True` (Agent should fill gap and retry).
- **Persistent/restart expectation:** Checkpoint remains `N`.
- **Protected invariant:** Strictly contiguous log admission.

#### Entry A06: Missing snapshot on unstarted stream
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` with `source_seq = 1` before any snapshot has been committed on the stream.
- **Precondition:** `checkpoint` is `None`.
- **Expected disposition:** `gap` with code `sequence_gap`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Stream remains uninitialized.
- **Retryability:** `True` (Agent must perform initial snapshot before emitting deltas).
- **Persistent/restart expectation:** No delta admitted without snapshot baseline.
- **Protected invariant:** Baseline snapshot prerequisite for delta streaming.

#### Entry A07: Immutable message content conflict
- **Classification:** `existing regression evidence`
- **Stimulus:** `ingest.delta` with `message.upsert` where `message_id` already exists in `account_messages` with different text, timestamp, sender, or direction.
- **Precondition:** `message_id` exists with `content_hash != incoming.content_hash`.
- **Expected disposition:** Exception `InvariantViolation("immutable message identifier has conflicting content")`; records conflict in `entity_conflicts`.
- **Expected state mutation:** Transaction rolls back; conflict logged in `entity_conflicts`.
- **Expected non-mutation:** Checkpoint does NOT advance; existing message in `account_messages` remains unmodified.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Rollback guarantees persistence integrity.
- **Protected invariant:** Absolute immutability of platform message records.

#### Entry A08: Chat platform identity conflict
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` with `chat.upsert` where `chat_id` exists in `account_chats`, but incoming `platform_user_id` differs from canonical `platform_user_id` (both non-null).
- **Precondition:** Existing chat has non-null `platform_user_id != incoming.platform_user_id`.
- **Expected disposition:** Exception `InvariantViolation("chat platform identity conflicts with canonical identity")`.
- **Expected state mutation:** Transaction rolls back; conflict recorded in `entity_conflicts`.
- **Expected non-mutation:** Checkpoint does not advance; canonical chat row unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Rollback preserves identity integrity.
- **Protected invariant:** Invariant pairing between chat ID and platform user identity.

#### Entry A09: Chat content conflict at equal timestamp
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` full chat where `upstream_updated_at == existing.upstream_updated_at` but `content_hash != existing.content_hash`.
- **Precondition:** Full chat exists with matching timestamp.
- **Expected disposition:** Exception `InvariantViolation("chat version has conflicting content")`.
- **Expected state mutation:** Transaction rollback.
- **Expected non-mutation:** Checkpoint does not advance; existing row preserved.
- **Retryability:** `False`.
- **Persistent/restart expectation:** No mutation.
- **Protected invariant:** Deterministic resolution; conflicting mutations at identical timestamp rejected.

#### Entry A10: Message referencing unknown or deleted parent chat
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` with `message.upsert` whose `chat_id` is missing from `account_chats` or has `is_deleted == 1`.
- **Precondition:** Parent chat absent or deleted.
- **Expected disposition:** Exception `InvariantViolation("message references an unknown or deleted chat")`.
- **Expected state mutation:** Transaction rollback; checkpoint not advanced.
- **Expected non-mutation:** Orphan message is never inserted into `account_messages`.
- **Retryability:** `False` (unless parent chat is restored/re-created prior to message).
- **Persistent/restart expectation:** No orphan messages in persistent store.
- **Protected invariant:** Strict referential integrity (messages must belong to extant chats).

#### Entry A11: Message tombstone parent chat conflict
- **Classification:** `existing regression evidence`
- **Stimulus:** `ingest.delta` with `message.delete` where `chat_id` differs from canonical `account_messages.chat_id` for that `message_id`.
- **Precondition:** Message exists canonically under a different `chat_id`.
- **Expected disposition:** Exception `InvariantViolation("message tombstone conflicts with canonical conversation")`.
- **Expected state mutation:** Transaction rollback; checkpoint unchanged.
- **Expected non-mutation:** Message is not deleted; tombstone not created.
- **Retryability:** `False`.
- **Persistent/restart expectation:** No inconsistent tombstones.
- **Protected invariant:** Cross-conversation deletion isolation.

#### Entry A12: Message tombstone identity reused with different conversation
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` with `message.delete` where a tombstone for `message_id` already exists in `entity_tombstones`, but specifies a different `chat_id`.
- **Precondition:** Tombstone exists in `entity_tombstones`.
- **Expected disposition:** Exception `InvariantViolation("tombstone identity was reused with a different conversation")`.
- **Expected state mutation:** Transaction rollback.
- **Expected non-mutation:** Checkpoint unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Consistent tombstones preserved across restart.
- **Protected invariant:** Tombstone identity consistency.

#### Entry A13: Resurrection attempt of deleted chat via delta
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` with `chat.upsert` for a `chat_id` present in `entity_tombstones`.
- **Precondition:** `chat_id` recorded in `entity_tombstones`.
- **Expected disposition:** `accepted` (sequence consumed), but merge operation is a no-op (`_tombstoned()` returns True).
- **Expected state mutation:** `raw_ingest_events` appended; checkpoint advances; `account_chats.is_deleted` remains `1`.
- **Expected non-mutation:** Chat is NOT resurrected; `is_deleted` stays `1`; canonical revision does NOT increment.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Entity remains deleted after reopen.
- **Protected invariant:** Absolute deletion barrier (tombstones prevent resurrection).

#### Entry A14: Resurrection attempt of deleted message via delta
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.delta` with `message.upsert` for a `message_id` present in `entity_tombstones`.
- **Precondition:** `message_id` recorded in `entity_tombstones`.
- **Expected disposition:** `accepted` (sequence consumed), but merge returns False (`_tombstoned()` returns True).
- **Expected state mutation:** `raw_ingest_events` appended; checkpoint advances.
- **Expected non-mutation:** `account_messages.is_deleted` remains `1`; message text not revived; canonical revision does not increment.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Entity remains deleted after reopen.
- **Protected invariant:** Message deletion barrier.

### 2.4 Bounded snapshot ingestion lifecycle

#### Entry N01: Valid snapshot begin frame
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.snapshot` with `frame_kind == "begin"`, `through_seq >= checkpoint`, expected record counts, `chunk_count >= 0`.
- **Precondition:** No other snapshot currently in `staging` state for this stream.
- **Expected disposition:** `accepted` with `next_expected_chunk_index = 0`.
- **Expected state mutation:** Row inserted into `snapshot_uploads` (`state='staging'`, `starting_checkpoint=checkpoint`, `begin_fingerprint`, record counts); stream created if not extant.
- **Expected non-mutation:** Checkpoint does not advance; canonical entity tables untouched.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Staging metadata persists across restart.
- **Protected invariant:** Bounded snapshot staging initialization.

#### Entry N02: Duplicate snapshot begin frame
- **Classification:** `normative behavior`
- **Stimulus:** Retransmission of `ingest.snapshot` begin with identical metadata and fingerprint.
- **Precondition:** Snapshot upload exists in `snapshot_uploads`.
- **Expected disposition:** `duplicate` with `next_expected_chunk_index` reflecting current upload progress.
- **Expected state mutation:** None.
- **Expected non-mutation:** Checkpoint and staging state unmutated.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Staging state maintained.
- **Protected invariant:** Idempotent snapshot initialization.

#### Entry N03: Conflicting snapshot begin frame
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.snapshot` begin reusing an existing `snapshot_id` with different record counts, `through_seq`, or `chunk_count`.
- **Precondition:** Snapshot upload exists with differing fingerprint.
- **Expected disposition:** `rejected` with code `chunk_conflict`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Upload record not overwritten; checkpoint unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Prior state preserved.
- **Protected invariant:** Snapshot identity immutability.

#### Entry N04: Concurrent snapshot staging rejection
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.snapshot` begin while another snapshot is already in `state == 'staging'`.
- **Precondition:** Uncommitted staging snapshot exists for this stream.
- **Expected disposition:** `rejected` with code `invariant_failed`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Existing staging upload untouched.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Existing staging upload unchanged across restart.
- **Protected invariant:** Single concurrent snapshot upload per stream.

#### Entry N05: Snapshot begin through_seq behind checkpoint
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.snapshot` begin with `through_seq < checkpoint`.
- **Precondition:** Checkpoint > `through_seq`.
- **Expected disposition:** `rejected` with code `invariant_failed`.
- **Expected state mutation:** None.
- **Expected non-mutation:** No row inserted in `snapshot_uploads`.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Persistent storage remains unchanged.
- **Protected invariant:** Snapshot high-water mark non-regression.

#### Entry N06: Valid ordered snapshot chunk frame
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.snapshot` chunk with `chunk_index == upload.next_chunk_index`, valid `entity_kind` ordering (chat -> message -> coverage_evidence), records count 1..100.
- **Precondition:** Upload in `staging` state; `chunk_index < upload.chunk_count`.
- **Expected disposition:** `accepted` with `next_expected_chunk_index = chunk_index + 1`.
- **Expected state mutation:** Row inserted in `snapshot_chunks`; records inserted into `snapshot_chat_records`, `snapshot_message_records`, or `snapshot_coverage_records`; `snapshot_uploads.next_chunk_index` incremented; received counts updated.
- **Expected non-mutation:** Checkpoint does NOT advance; canonical entity tables (`account_chats`, `account_messages`) NOT mutated.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Staged chunk rows persist across restart.
- **Protected invariant:** Staging isolation (data in staging is invisible to canonical reads).

#### Entry N07: Duplicate snapshot chunk replay
- **Classification:** `normative behavior`
- **Stimulus:** Retransmission of snapshot chunk with identical `chunk_index`, `entity_kind`, and records fingerprint.
- **Precondition:** Chunk already recorded in `snapshot_chunks`.
- **Expected disposition:** `duplicate` with `next_expected_chunk_index = upload.next_chunk_index`.
- **Expected state mutation:** None.
- **Expected non-mutation:** No duplicate records inserted.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Idempotent replay.
- **Protected invariant:** Chunk-level idempotency.

#### Entry N08: Conflicting snapshot chunk frame
- **Classification:** `existing regression evidence`
- **Stimulus:** Snapshot chunk presenting a `chunk_index` already staged, but with different records or differing `entity_kind`.
- **Precondition:** Chunk exists with different fingerprint.
- **Expected disposition:** `rejected` with code `chunk_conflict`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Staged chunk records not overwritten.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Staged material uncorrupted.
- **Protected invariant:** Chunk immutability.

#### Entry N09: Snapshot chunk sequence gap
- **Classification:** `normative behavior`
- **Stimulus:** Snapshot chunk where `chunk_index != upload.next_chunk_index`.
- **Precondition:** Chunk arrives out of order (e.g. chunk 2 when chunk 1 expected).
- **Expected disposition:** `gap` with code `sequence_gap`, `retryable = True`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Staging tables unmutated; `next_chunk_index` unchanged.
- **Retryability:** `True` (Agent should send expected chunk).
- **Persistent/restart expectation:** Gap rejected cleanly.
- **Protected invariant:** Strictly contiguous chunk ingestion.

#### Entry N10: Snapshot entity kind ordering violation
- **Classification:** `existing regression evidence`
- **Stimulus:** Snapshot chunk with `entity_kind` violating monotonic order (e.g. `chat` chunk arriving after `message` chunk was already staged).
- **Precondition:** `kind_order[payload.entity_kind] < kind_order[upload.last_entity_kind]`.
- **Expected disposition:** `rejected` with code `invariant_failed`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Out-of-order chunk rejected.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Staging tables remain uncorrupted across restart.
- **Protected invariant:** Deterministic snapshot staging topological order.

#### Entry N11: Snapshot chunk without begin frame
- **Classification:** `normative behavior`
- **Stimulus:** `ingest.snapshot` chunk for a `snapshot_id` not present in `snapshot_uploads`.
- **Precondition:** Upload does not exist.
- **Expected disposition:** `rejected` with code `snapshot_incomplete`.
- **Expected state mutation:** None.
- **Expected non-mutation:** No chunk inserted.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Persistent storage remains unchanged.
- **Protected invariant:** Snapshot lifecycle sequencing (begin must precede chunks).

#### Entry N12: Oversized snapshot chunk frame
- **Classification:** `normative behavior`
- **Stimulus:** Snapshot chunk frame whose encoded JSON size exceeds 512 KiB (`MAX_SNAPSHOT_FRAME_BYTES`).
- **Precondition:** Raw payload size > 524,288 bytes.
- **Expected disposition:** Exception `InvariantViolation("snapshot frame exceeds 512 KiB")`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Frame rejected before staging.
- **Retryability:** `False`.
- **Persistent/restart expectation:** No malformed or oversized frame written to disk.
- **Protected invariant:** Bounded memory and frame size limits.

#### Entry N13: Oversized individual snapshot record
- **Classification:** `normative behavior`
- **Stimulus:** Snapshot chunk containing an individual record exceeding 384 KiB (`MAX_SNAPSHOT_RECORD_BYTES`).
- **Precondition:** Single record size > 393,216 bytes.
- **Expected disposition:** Validation rejection during protocol model validation.
- **Expected state mutation:** None.
- **Expected non-mutation:** Frame dropped before repository admission.
- **Retryability:** `False`.
- **Persistent/restart expectation:** No oversized record admitted to disk.
- **Protected invariant:** Bounded record size limits.

#### Entry N14: Valid snapshot commit
- **Classification:** `existing regression evidence`
- **Stimulus:** `ingest.snapshot` commit with `chunk_count == upload.chunk_count`.
- **Precondition:** Upload in `staging` state; all expected chunks and record counts match staged totals; `starting_checkpoint == current_checkpoint`.
- **Expected disposition:** `accepted` with `snapshot_committed = True`.
- **Expected state mutation:** In single transaction: staged chats merged to `account_chats`; staged messages merged to `account_messages`; coverage applied; `committed_snapshots` recorded; `stream_chat_membership` and `stream_message_membership` replaced for this stream; `ingest_checkpoints.checkpoint` advanced to `through_seq`; `snapshot_uploads.state` set to `'committed'`; staging records purged from `snapshot_chat_records`, `snapshot_message_records`, `snapshot_coverage_records`; canonical revision incremented; `projection_work` queued.
- **Expected non-mutation:** Tombstoned entities are NOT resurrected; conflicting records raise invariant violation and rollback.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Full committed state survives restart; staging tables empty.
- **Protected invariant:** Atomic snapshot commit; checkpoint advancement; staging cleanup.

#### Entry N15: Duplicate snapshot commit replay
- **Classification:** `existing regression evidence`
- **Stimulus:** Retransmission of snapshot commit for an already committed snapshot.
- **Precondition:** `upload.state == 'committed'`.
- **Expected disposition:** `duplicate` with `snapshot_committed = True`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Checkpoint, canonical tables, and membership unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Idempotent replay.
- **Protected invariant:** Snapshot commit idempotency.

#### Entry N16: Incomplete snapshot commit attempt
- **Classification:** `normative behavior`
- **Stimulus:** Snapshot commit arriving when staged chunks or record counts do not match begin declarations.
- **Precondition:** `next_chunk_index != chunk_count` or received record counts != expected record counts.
- **Expected disposition:** `rejected` with code `snapshot_incomplete`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Checkpoint does NOT advance; staging records remain staged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Incomplete staging uncommitted; preserved across restart.
- **Protected invariant:** Atomicity and completeness of snapshot state.

#### Entry N17: Snapshot commit chunk count conflict
- **Classification:** `normative behavior`
- **Stimulus:** Snapshot commit specifying `chunk_count` differing from `upload.chunk_count` declared at begin.
- **Precondition:** Snapshot upload exists in staging.
- **Expected disposition:** `rejected` with code `chunk_conflict`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Staging unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Staging state unchanged across restart.
- **Protected invariant:** Snapshot manifest agreement.

#### Entry N18: Checkpoint race during snapshot staging
- **Classification:** `existing regression evidence`
- **Stimulus:** Snapshot commit executed when `current_checkpoint != upload.starting_checkpoint`.
- **Precondition:** Concurrent delta advanced the stream checkpoint while snapshot was being uploaded.
- **Expected disposition:** `rejected` with code `invariant_failed`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Snapshot NOT committed; checkpoint stays at new value.
- **Retryability:** `False` (requires fresh snapshot begin from new checkpoint).
- **Persistent/restart expectation:** Checkpoint race causes clean rejection without mutating disk.
- **Protected invariant:** Snapshot consistency isolation against stream checkpoint drift.

#### Entry N19: Snapshot staging isolation
- **Classification:** `normative behavior`
- **Stimulus:** Query canonical read model while snapshot upload is in `staging` state.
- **Precondition:** Chunks 0..K staged in `snapshot_chat_records` / `snapshot_message_records`.
- **Expected disposition:** Staged records are completely invisible to `HistoryAnalyticsSource.account_read_model()` and `HistoryRepository.conversation_exists()`.
- **Expected state mutation:** None.
- **Expected non-mutation:** Canonical tables reflect only prior committed revision.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Staging isolation maintained across reopen until committed.
- **Protected invariant:** Staging read isolation.

#### Entry N20: Snapshot resurrection prevention
- **Classification:** `normative behavior`
- **Stimulus:** Snapshot chunk contains chat or message records for entities present in `entity_tombstones`.
- **Precondition:** Entities tombstoned in `entity_tombstones`.
- **Expected disposition:** Snapshot commit succeeds, but staged records matching `entity_tombstones` are filtered out during merge via `LEFT JOIN entity_tombstones ... WHERE t.entity_id IS NULL`.
- **Expected state mutation:** Active canonical entities merged; tombstoned entities remain tombstoned (`is_deleted=1`).
- **Expected non-mutation:** Deleted entities NOT revived.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Tombstone barrier persists across restart.
- **Protected invariant:** Deletion barrier holds across bulk snapshot admission.

### 2.5 Agent durable delivery transitions

#### Entry AG01: Agent enqueue semantic change
- **Classification:** `normative behavior`
- **Stimulus:** Agent calls `outbox.enqueue(change)`.
- **Precondition:** Change contains novel or updated material.
- **Expected disposition:** Returns enqueued outbox item.
- **Expected state mutation:** `meta.last_source_seq` incremented by 1; outbox record stored with `source_seq = last_source_seq`; entity store updated; `meta.outbox_count` incremented.
- **Expected non-mutation:** `acknowledged_source_seq` unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Outbox item persists in IndexedDB.
- **Protected invariant:** Monotonic Agent source sequence allocation.

#### Entry AG02: Agent enqueue semantic no-op
- **Classification:** `normative behavior`
- **Stimulus:** Agent calls `outbox.enqueue(change)` with entity material identical to stored entity (`normalizedMaterialEqual` returns True).
- **Precondition:** Entity exists in store with matching canonical material.
- **Expected disposition:** Returns `null`.
- **Expected state mutation:** None.
- **Expected non-mutation:** `meta.last_source_seq` NOT incremented; NO record written to outbox; `meta.outbox_count` unchanged.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Outbox remains unaffected.
- **Protected invariant:** Semantic no-ops do not consume sequence numbers.

#### Entry AG03: Atomic message enqueue with placeholder parent
- **Classification:** `normative behavior`
- **Stimulus:** Agent calls `outbox.enqueueMessageWithParent(messageChange, parentChange)`.
- **Precondition:** Matching `chat_id`.
- **Expected disposition:** Atomic commit of parent chat (if not extant) and message.
- **Expected state mutation:** If parent chat is new, allocates sequence `N`, message allocates `N+1`; both stored in outbox and respective stores within one transaction.
- **Expected non-mutation:** No partial insertion if validation fails.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Outbox transaction commits atomically in IndexedDB.
- **Protected invariant:** Dependency-closed acquisition before outbound transmission.

#### Entry AG04: Ingestion acknowledgement prefix trimming
- **Classification:** `normative behavior`
- **Stimulus:** Agent receives `ingest.ack` with `committed_source_seq`.
- **Precondition:** `committed_source_seq <= meta.last_source_seq`.
- **Expected disposition:** `acknowledged`.
- **Expected state mutation:** `meta.acknowledged_source_seq = max(meta.acknowledged_source_seq, committed_source_seq)`; outbox records with `key <= newCheckpoint` deleted from IndexedDB; `meta.outbox_count` decremented.
- **Expected non-mutation:** Unacknowledged items (`source_seq > committed_source_seq`) preserved.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Trimming is durable across restart.
- **Protected invariant:** Bounded outbox queue size; at-least-once delivery guarantee.

#### Entry AG05: Agent snapshot preparation
- **Classification:** `normative behavior`
- **Stimulus:** Agent triggers `outbox.createSnapshot(snapshotId)`.
- **Precondition:** No pending snapshot in building state.
- **Expected disposition:** Manifest initialized.
- **Expected state mutation:** Manifest stored in `snapshotManifests` (`state='building'`, `through_seq = meta.last_source_seq`); `snapshotChunks` and `snapshotOverrides` cleared; `meta.pending_snapshot` set.
- **Expected non-mutation:** Entity stores and outbox unmutated.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Building snapshot manifest persists in IndexedDB.
- **Protected invariant:** Point-in-time snapshot boundary.

#### Entry AG06: Agent progressive snapshot chunk building
- **Classification:** `normative behavior`
- **Stimulus:** Agent calls `outbox.buildNextSnapshotChunk()`.
- **Precondition:** Manifest in `building` state.
- **Expected disposition:** Emits chunk (1..100 records) bounded to <= 458,752 target bytes.
- **Expected state mutation:** Chunk stored in `snapshotChunks`; manifest pointers advanced across stores (chats -> messages -> coverage evidence).
- **Expected non-mutation:** Live mutations arriving during building are diverted to `snapshotOverrides` without corrupting snapshot data.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Staged chunks persist in IndexedDB.
- **Protected invariant:** Chunk sizing limits; non-blocking live operation during snapshot generation.

#### Entry AG07: Agent snapshot commit acknowledgement
- **Classification:** `normative behavior`
- **Stimulus:** Agent receives `ingest.ack` with `snapshotProgress.committed == True`.
- **Precondition:** Manifest in `ready` state; `committedSourceSeq >= manifest.through_seq`.
- **Expected disposition:** Snapshot acknowledged.
- **Expected state mutation:** Staged chunks deleted from `snapshotChunks`; manifest deleted; `snapshotOverrides` cleared; `meta.pending_snapshot = null`; outbox trimmed through `committedSourceSeq`.
- **Expected non-mutation:** Entity stores retain their latest state.
- **Retryability:** `False`.
- **Persistent/restart expectation:** Outbox trimming and staging cleanup persist in IndexedDB.
- **Protected invariant:** Snapshot delivery completion and staging reclamation.

#### Entry AG08: Agent reconnect and outbox replay prefix
- **Classification:** `existing regression evidence`
- **Stimulus:** Agent WebSocket reconnects and receives `agent.session` with `committed_source_seq = K`.
- **Precondition:** Outbox contains records from `K + 1` to `last_source_seq`.
- **Expected disposition:** Agent retransmits unacknowledged records in strictly contiguous ascending sequence order.
- **Expected state mutation:** Session state updated.
- **Expected non-mutation:** No gaps in replay stream; no records omitted.
- **Retryability:** `True`.
- **Persistent/restart expectation:** Unacknowledged replay prefix survives restart in IndexedDB.
- **Protected invariant:** Lossless durable replay across connection drops.

---

## 3. Quality scenarios

The six mandatory quality scenarios from Section 11 are defined with explicit stimulus, environment, required response, and response measures:

| Scenario | Quality attribute | Stimulus / Environment | Required response | Response measure |
|---|---|---|---|---|
| **QS-1** | Canonical integrity | `source_seq = N + 2` arrives at Brain while committed checkpoint is `N`. | Reject frame with `code="sequence_gap"`, `retryable=True`; do not mutate state. | Checkpoint stays `N`; canonical digest and revision unchanged; error details specify expected sequence `N + 1`. |
| **QS-2** | Ingestion idempotency | Committed event is retransmitted by Agent after dropped network ACK. | Identify duplicate by `(event_id, fingerprint)`; return status `duplicate`. | Canonical semantic state, checkpoint, and revision unchanged; no duplicate entity or event row created. |
| **QS-3** | Snapshot integrity | Staged chunk index `I` is retransmitted with conflicting material or invalid kind order. | Reject conflicting chunk with `code="chunk_conflict"` or `code="invariant_failed"`. | Staging tables uncorrupted; no partial canonical mutation; next expected chunk index unchanged. |
| **QS-4** | Deletion closure | Deleted entity returns via stale delta, snapshot chunk, reconnect replay, or repair stream. | Deletion barrier (`entity_tombstones`) suppresses resurrection. | Deleted entity absent from canonical read model (`HistoryAnalyticsSource`), `is_deleted` remains 1; revision not incremented. |
| **QS-5** | Storage durability | Backend process terminates abruptly after accepted transaction; repository reconstructed on reopen. | Reopen same encrypted SQLite file; recover exact committed state. | Checkpoint, event log, canonical entities, tombstones, and revision equal pure reference model. |
| **QS-6** | Agent delivery durability | Agent disconnects, restarts background worker, or drops connection mid-flight. | Durable outbox (`IndexedDB`) preserves exact replay prefix from `acknowledged_source_seq + 1`. | Zero lost events; zero out-of-order deliveries; no re-allocation of sequence numbers for existing items. |

---

## 4. Independent reference models

To prevent circular reasoning, Task 5 requires independent reference models that do not import production persistence or merge code.

### 4.1 Brain independent reference model (`tests/state_models/brain_ingestion_model.py`)

- **Scope:** Pure in-memory representation tracking:
  - `checkpoint`: integer high-water sequence mark per stream.
  - `canonical_revision`: integer account revision.
  - `chats`: dictionary mapping `chat_id` to canonical chat value.
  - `messages`: dictionary mapping `message_id` to canonical message value.
  - `tombstones`: set of `(kind, entity_id)` pairs.
  - `committed_events`: dictionary mapping `event_id` to `(source_seq, fingerprint)`.
  - `pending_snapshot`: structure tracking staged snapshot identity, chunk indices, and staged entity sets.
- **Independence rules:**
  - MUST NOT import `HistoryRepository` or `IngestionService`.
  - MUST NOT execute SQL statements or import production SQL definitions.
  - MUST NOT call `_merge_chat`, `_merge_message`, or `_delete_entity`.
  - MUST NOT use production fingerprinting routines directly if doing so masks transition errors.
  - MAY import protocol value types (`RawChat`, `RawMessage`, `StreamKey`) for data carrier purposes only.

### 4.2 Agent independent reference model (`tests/state_models/agent_delivery_model.py`)

- **Scope:** Pure reference model tracking:
  - `account_epoch`: integer epoch.
  - `last_source_seq`: highest allocated source sequence.
  - `acknowledged_source_seq`: highest confirmed sequence from Brain.
  - `outbox_items`: ordered list of unacknowledged delivery items.
  - `stored_entities`: dictionary of local entity state.
  - `pending_snapshot`: active snapshot building state.
- **Harness architecture:**
  - The real JavaScript implementation (`DurableIngestOutbox`, `entity-merge.mjs`) is driven via a thin persistent Node command harness (`extension/qualification/ingestion-model-harness.mjs`).
  - Python tests issue JSON-line commands over stdin and receive structured state snapshots over stdout.
  - Node process remains alive for the duration of a generated test case to avoid per-step process creation overhead.
  - DO NOT reimplement the JavaScript outbox in Python and claim it qualifies production Agent behavior.

---

## 5. Per-transition oracle and permanent falsifiers

### 5.1 Oracle comparison points

After **every** generated state machine transition, the test harness evaluates:
1. `disposition`: `accepted`, `duplicate`, `gap`, or `rejected`.
2. `checkpoint`: current stream sequence number.
3. `rejection_code`: error code matching protocol schema (`sequence_gap`, `chunk_conflict`, `invariant_failed`, etc.).
4. `retryable`: boolean retry indicator.
5. `canonical_revision`: incremented if and only if semantic state changed.
6. `logical_entities`: complete match of active chats, messages, and tombstones between production store and reference model.
7. `staging_isolation`: assert staging tables contain zero uncommitted items visible to canonical readers.
8. `non_mutation`: on `rejected`, `duplicate`, or `gap`, verify before-state equals after-state.

### 5.2 Metamorphic history equivalence

The oracle asserts metamorphic equivalence across valid execution variants:
$$\text{CleanHistory} \equiv \text{History} + \text{DuplicateFrames} \equiv \text{History} + \text{LostAckRetry} \equiv \text{History} + \text{ReopenCutPoints}$$

### 5.3 Permanent oracle falsifiers

The test suite must include four permanent broken test doubles/adapters proving that the oracle actively catches violations:

1. **`BrokenGapAdapter`:** Advances the stream checkpoint upon receiving a sequence gap.
   - *Target failure:* Catches improper sequence advancement on non-contiguous frames.
2. **`BrokenDuplicateAdapter`:** Mutates state or increments canonical revision when processing an exact duplicate event.
   - *Target failure:* Catches idempotency regressions.
3. **`BrokenDeletionAdapter`:** Allows a tombstoned entity to be updated or resurrected by a subsequent delta or snapshot.
   - *Target failure:* Catches deletion closure and resurrection regressions.
4. **`BrokenReopenAdapter`:** Discards or drops committed checkpoint / entity state across repository close and reopen.
   - *Target failure:* Catches persistence and durability loss across process boundaries.

---

## 6. Persistent runtime and SQLite qualification design

### 6.1 Required production SQLite profile

Authoritative local Brain persistence requires the following audited SQLite / SQLCipher profile:

```text
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;           -- (value 2)
PRAGMA foreign_keys = ON;            -- (value 1)
PRAGMA busy_timeout = 5000;          -- (or 7500 ms)
PRAGMA cipher_memory_security = OFF; -- (Windows packaging compatibility)
PRAGMA cipher_plaintext_header_size = 0;
```

- **Encryption:** SQLCipher 256-bit AES encryption with 32-byte device-bound key managed by `LocalDataKey` / DPAPI.
- **Concurrency & Writer Serialization:** `app/persistence/database.py` only shows in-process `_connection_locks` around connection open/close transitions and accounting, alongside an exclusive lock during schema migrations and lifecycle changes. Individual transaction execution (`BEGIN IMMEDIATE` / `COMMIT`) occurs outside those locks on fresh `check_same_thread=False` connections, relying on SQLite's internal `BEGIN IMMEDIATE` writer serialization.
- **Multi-connection & Checkpoint Profile:** Multiple concurrent application connections/threads and active SQLite WAL checkpoint behavior operate concurrently and remain relevant, active, and unexcluded from the runtime profile.
- **Connection Model:** Native connections are wrapped in `_TrackedConnection` to maintain reference counts and enforce transition accounting.

### 6.2 Distinction between local development and shipped package

- Local Python execution in development uses Python 3.13.6, `sqlcipher3==0.6.2`, linked to SQLite 3.51.1 with SQLCipher 4.12.0 community (dated local probe: 2026-09-09).
- The production shipped product is packaged via PyInstaller on Windows using Python 3.11 as specified in `.github/workflows/ci.yml` and `packaging/build-windows.ps1`.
- Local Python test runs qualify algorithmic state machine transitions; they do not constitute release qualification of the packaged Windows Store binary. Actual packaged Windows and PR-runner native versions must be measured dynamically during Task 5C / PR 5B.

### 6.3 SQLite durability advisory research and qualification status

- **Dated Local Observation (2026-09-09):** Local environment probe records Python 3.13.6, `sqlcipher3==0.6.2`, SQLite 3.51.1, and SQLCipher 4.12.0 community.
- **Upstream Advisory Evaluation:**
  - The official SQLite WAL-reset advisory identifies a database corruption risk affecting SQLite versions 3.7.0 through 3.51.2, fixed in SQLite 3.51.3 (with backports 3.44.6 and 3.50.7). Sources: [SQLite WAL Reset Bug](https://sqlite.org/wal.html#walresetbug) and [SQLite Release 3.51.3](https://sqlite.org/releaselog/3_51_3.html).
  - The failure trigger requires WAL mode combined with two or more connections to the same database across separate threads or processes exhibiting concurrent write and checkpoint behavior.
  - SQLCipher 4.14.0 incorporates SQLite 3.51.3 and strongly recommends that all WAL-mode applications upgrade: [SQLCipher 4.14.0 Release](https://github.com/sqlcipher/sqlcipher/releases/tag/v4.14.0).
  - In the current codebase, `app/persistence/database.py` enforces WAL mode (`PRAGMA journal_mode = WAL`) and synchronous FULL (`PRAGMA synchronous = FULL`), opening fresh `check_same_thread=False` connections per read or transaction. In-process `_connection_locks` guard connection transition accounting and exclusive lifecycle phases, but individual transaction `BEGIN IMMEDIATE` and commit operations execute outside those locks. While SQLite `BEGIN IMMEDIATE` provides writer serialization, multiple concurrent application connections/threads and active SQLite checkpoint behavior remain relevant and unexcluded.
  - Consequently, the local development profile cannot be qualified as unaffected by the upstream advisory. (No claim is made that corruption has occurred).
- **Production Qualification Requirement:**
  - Declaring file-backed Tier B storage production-equivalent remains blocked until an upgrade to a fixed runtime (SQLite $\ge 3.51.3$ / SQLCipher $\ge 4.14.0$) and/or a rigorous proof of trigger exclusion is established.
  - Specific version numbers and advisory ranges are dated observations and must not be encoded as timeless architecture policy; current upstream advisories must be re-evaluated against the deployed runtime at implementation time.

---

## 7. CI baseline and shrinking benchmark design

### 7.1 Named calibration profiles

To avoid uncontrolled CI runtime expansion, named profiles are defined for stateful Hypothesis generation. These counts represent **initial starting calibration inputs**, not permanent normative values:

| Profile | Suite | Target examples × steps | Backend | Focus |
|---|---|---:|---|---|
| **Tier A General** | Brain Ingestion | 90 × 40 | In-memory | Broad state-space exploration |
| **Tier A Deletion** | Brain Ingestion | 60 × 30 | In-memory | Tombstone and resurrection stress |
| **Agent Tier A General** | Agent Outbox | 50 × 30 | Persistent Node harness | Durable outbox and framing semantics |
| **Agent Tier A Deletion** | Agent Outbox | 30 × 20 | Persistent Node harness | Deletion, reconnect, and snapshot stress |
| **Tier B General** | Brain SQLite | 15 × 25 | File-backed SQLCipher | Transaction, reopen, and durability |
| **Tier B Deletion** | Brain SQLite | 10 × 20 | File-backed SQLCipher | Durable deletion closure across reopen |
| **Windows Smoke** | Persistence Factory | ~5 × 12 + regressions | Production LocalSQLite | Windows-specific filesystem semantics |

If CI budget pressure requires tuning, example counts must be reduced before removing critical transition families or invariant assertions.

### 7.2 Deletion profile guarantee

Informal transition probability is insufficient to stress deletion boundaries. At least **40% of generated PR histories** must run under dedicated deletion-focused profiles, where entity creation is forced early followed by adversarial replay, stale delta delivery, snapshot re-ingestion, and restart cut points.

### 7.3 Shrinking benchmark protocol

When a stateful invariant fails, Hypothesis shrinks the operation trace to a minimal sequence. The benchmark must record:
- Wall-clock duration of shrinking phase.
- Number of transitions in minimized reproducer.
- Reproduction trace converted into permanent deterministic regression in `tests/test_history_v2.py`.

### 7.4 Response measure and budget thresholds

- **Target:** State machine suites (Tasks 5 and 6 combined) must add $\le 5\%$ to the p95 PR critical-path duration on standard runners.
- **Hard review threshold:** $> 10\%$ added p95 PR duration requires explicit engineering trade-off review and profile downsizing.
- **Provisional Historical Baseline:** A provisional sample of 15 first-attempt successful PR workflow runs from 2026-09-02 through 2026-09-04 exhibited a p95 elapsed duration of 1704.7 seconds, with Windows execution on the critical path (only 6 of these runs matched the exact current workflow revision; this is provisional historical context, not closure evidence).
- **Dynamic Measurement:** Execution duration, example counts, transitions executed, SQLite reopens, failure shrink durations, runner OS, and Python/SQLite/Hypothesis versions must be captured dynamically from the runner environment.

---

## 8. Unresolved normative discrepancies and gate readiness ledger

### 8.1 Discrepancy between in-memory `IngestionService` and authoritative `HistoryRepository`

1. **Snapshot requirements:** In-memory `IngestionService` (`app/transport/ingestion.py`, line 283) enforces that a complete monolithic snapshot must precede any delta. In contrast, authoritative `HistoryRepository` (`app/persistence/history.py`) supports progressive chunked staging (`begin_snapshot`, `add_snapshot_chunk`, `commit_snapshot`).
2. **Coverage evidence:** `HistoryRepository` natively ingests and persists historical coverage evidence (`coverage.observed`, `CoverageEvidence` types). `IngestionService` does not support coverage evidence.
3. **Tombstone tables:** `HistoryRepository` records permanent tombstones in `entity_tombstones` with stream epoch and source sequence. `IngestionService` pops deleted entities from in-memory dictionaries without persistent tombstone barriers.

*Disposition:* `HistoryRepository` is the sole canonical persistence authority. `IngestionService` is a legacy in-memory helper and must not be used as the reference oracle for canonical persistence.

### 8.2 PR 5R research gate closure criteria

PR 5R is a research and specification gate. It closes when:

1. [x] **Ingestion State Transition Catalogue Established:** Ingestion transition catalogue (S01–S03, D01–D09, A01–A14, N01–N20, AG01–AG08, exactly 54 entries) is established with all required specification fields present.
2. [x] **Quality Scenarios Defined:** Scenarios QS-1 to QS-6 map explicitly to catalogue transition paths and independent reference model boundaries.
3. [x] **Oracle Falsifier Specifications Complete:** Permanent broken adapters (`BrokenGapAdapter`, `BrokenDuplicateAdapter`, `BrokenDeletionAdapter`, `BrokenReopenAdapter`) are specified.
4. [x] **Persistent Runtime Qualification Designed:** SQLite production configuration profile and dated durability advisory status (2026-09-09) are documented.
5. [x] **CI Shrinking Benchmark Designed:** Named calibration profiles, $\le 5\%$ p95 budget target, and failure shrinking protocols are specified.
6. [x] **Static and Structural Verification Passed:** Verification tooling confirms that cited file paths, selected AST symbols, tables, and schema constants exist in the codebase, required contract structures and counts are present, and banned fabricated patterns are absent. (Static checks verify structural and AST existence; full semantic and source truth requires human architectural review and downstream executable Task 5 and Task 6 oracles).

### 8.3 Downstream implementation and qualification readiness ledger

The following deliverables are downstream tasks authorized by PR 5R; they do not block PR 5R research gate closure:

- **Task 5A (PR 5A) Readiness:** Brain independent reference model (`tests/state_models/brain_ingestion_model.py`) and Tier A state machine implementation (`tests/stateful/test_brain_ingestion.py`).
- **Task 5C (PR 5B) Readiness:** File-backed persistent Brain qualification (`tests/state_models/sqlite_brain_adapter.py`), reopen/reconstruction qualification, benchmark calibration, and packaged runtime qualification.
  *Note:* Packaged runtime qualification blocks declaring Tier B production-equivalent and final Task 5C / Task 9 closure; it does not block Task 5A or Task 6A design.
- **Task 5B (PR 5C) Readiness:** Real JavaScript Agent Node qualification harness (`extension/qualification/ingestion-model-harness.mjs`) and durable delivery qualification.
- **Tasks 6A / 6B Readiness:** Analytics determinism and rebuild convergence implementation.
