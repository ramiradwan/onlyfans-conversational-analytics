# ADR 0004: Use durable cursors, idempotent deltas, and explicit resynchronization

- Status: accepted

## Context and problem statement

MV3 service workers, WebSockets, and Brain processes are interruptible. Network delivery can be duplicated or lost, and in-memory timers and queues disappear on restart. Agent and Brain need durable progress markers, while Bridge needs an explicit way to detect and repair gaps in its derived view.

## Decision drivers

- No captured delta may disappear merely because the worker or socket was unavailable.
- Retries must be safe; “exactly once over WebSocket” is not assumed.
- A snapshot and the deltas around it need an explicit ordering boundary.
- Brain replicas and restarts must agree on ingestion progress.
- Bridge must detect missed derived updates and recover without Agent access.
- MV3 worker termination must be a normal lifecycle event.

## Considered options

### Send a full snapshot on every reconnect

This is simple and eventually replaces stale state.

Trade-offs: it is expensive, can overwrite newer work without fencing, does not define deltas captured during snapshot construction, and still loses offline deltas if the local snapshot is incomplete.

### Best-effort deltas with in-memory queues

This minimizes persistent coordination.

Trade-offs: worker, Brain, or socket restart loses ordering state; duplicates and gaps are undetectable; horizontal Brain scaling is unsafe.

### At-least-once delivery with durable cursors and explicit resync

Agent persists captured events, Brain persists checkpoints and deduplicates, and Bridge consumes a revisioned read model.

Trade-off: it adds storage, acknowledgments, sequence rules, and recovery states.

## Decision outcome

Choose **at-least-once delivery with durable cursors, idempotent effects, and explicit resync**.

### Agent source stream

- For each `creator_account_id`, Agent persists an `agent_stream_id` and monotonically increasing `source_seq`. The stream identifier changes only when that account's local capture store is deliberately reset or cannot be reconciled.
- Agent persists each captured ingest event, stable `event_id`, and sequence before attempting network delivery. Unacknowledged events form an account-partitioned outbox that survives worker termination.
- `ingest.snapshot` contains a stable `snapshot_id`, the source stream, and `through_seq`. It is a transactionally consistent full raw view as of that high-water mark. Events after the mark remain ordered deltas.
- `ingest.delta` contains one typed raw change, `event_id`, source stream, and sequence. A domain record identifier alone is not the delivery idempotency key because one record may be updated more than once.
- Presence observations are excluded from this outbox because their freshness, not eventual replay, is meaningful (ADR 0002).

### Brain ingestion and recovery

- Agent's hello reports the source stream and last locally recorded acknowledgment. Brain compares these with a checkpoint stored in a durable, shared store.
- Brain returns either resume instructions or `sync.required`. An unknown stream, missing checkpoint, irrecoverable gap, local reset, or failed invariant requires a snapshot; an ordinary reconnect resumes from the highest contiguous Brain acknowledgment.
- Brain accepts writes only with the current Agent fencing token from ADR 0003.
- Snapshot replacement and checkpoint advancement are atomic from the protocol's perspective. Deltas through `through_seq` are already represented by the snapshot; only later sequences are applied.
- Brain enforces uniqueness on the account, Agent installation, source stream, and event/sequence identity. Duplicate snapshots and deltas return the existing acknowledgment without repeating derived effects.
- Brain sends `ingest.ack` only after raw data and the durable checkpoint commit. Delivery is at least once; processing effects are idempotent.
- Retryable failures leave the checkpoint unchanged. Non-retryable validation failures identify the rejected item and stop contiguous advancement until Agent repairs, quarantines with an explicit policy, or performs a required resync. Silent skipping is forbidden.

### Bridge read-model stream

- Every accepted Bridge connection receives a Brain-owned `state.snapshot` with a `view_revision`; bootstrap is not conditional on development mode or local Agent detection.
- Brain publishes each atomic derived change set as one ordered `state.delta` with the next revision. Conversation and analytics changes caused by one ingest commit are not exposed as independently ordered messages.
- Bridge applies only the next revision. A duplicate is ignored; a gap or invalid delta triggers `state.resync` and Bridge does not claim realtime state until a replacement snapshot arrives.
- Brain may later add bounded replay as an optimization. Protocol v1 may always answer Bridge reconnect/resync with a full state snapshot.

### MV3 lifecycle behavior

- The worker treats its socket and all timers as disposable. Durable identity, stream, outbox, acknowledgments, and configuration live outside worker memory.
- Startup, installation, tab/page messages, alarms if configured, and other wake-capable extension events call an idempotent `ensureConnected` routine. Reconnect backoff avoids storms, but a vanished `setTimeout` cannot strand durable work because the next wake event resumes it.
- Capture is persisted before network send. A worker may terminate at any instruction boundary and safely resend after restart.
- Heartbeats can maintain a liveness lease while the worker is active; they are not a substitute for resync or durable progress.

## Consequences

### Positive

- Agent, Brain, and Bridge can each restart without an undetectable data gap.
- Duplicate sends are expected and safe.
- Snapshot/delta races have one high-water-mark rule.
- Bridge recovery no longer depends on Agent or a special development path.
- Brain can scale horizontally when checkpoints and deduplication are shared.

### Negative

- Agent needs transactional local persistence and outbox compaction.
- Brain needs durable checkpoint, uniqueness, and read-model revision storage.
- Poison-event handling becomes an explicit operational workflow.
- Full Bridge snapshots may be costly until bounded replay is introduced.

## Confirmation

- Kill the Agent worker after local persistence but before send, and after send but before acknowledgment; both tests must converge without duplicate effects.
- Restart Brain between adjacent deltas and verify the durable contiguous checkpoint.
- Capture a delta during snapshot construction and verify it is represented exactly once according to `through_seq`.
- Drop a Bridge delta and verify that the revision gap forces resync before realtime status returns.
- Run with Bridge closed and later connect; the initial state snapshot must be complete.
