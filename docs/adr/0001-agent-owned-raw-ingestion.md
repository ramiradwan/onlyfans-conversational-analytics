# ADR 0001: Make Agent the sole raw-ingestion producer

- Status: accepted

## Context and problem statement

Raw chat snapshots and deltas require one authoritative producer. Allowing both Agent and Bridge to submit the same data creates competing lifecycles, payloads, and identities, and makes account binding and recovery ambiguous. Agent is closest to capture and remains active independently of the Bridge UI, while Brain provides the durable validation and persistence boundary.

## Decision drivers

- Exactly one network producer for each raw-data message type.
- Ingestion must continue while Bridge is closed.
- Raw data must be bound to the Agent's authenticated account and source stream.
- Bridge must not require privileged access to extension storage.
- Brain must be able to validate, persist, deduplicate, and acknowledge ingestion before publishing derived state.

## Considered options

### Agent is the sole raw-ingestion producer

Agent is closest to capture, remains useful without Bridge, and can persist an outbox across MV3 worker restarts. Brain accepts raw ingest only on an Agent-bound session.

Trade-off: Agent needs durable sequencing, retry, and snapshot construction rather than relying on Bridge to trigger recovery.

### Bridge is the sole raw-ingestion producer

This would give the UI one place to initiate synchronization.

Trade-offs: ingestion stops when the UI is closed, Bridge needs a privileged and browser-specific Agent API, and two independent socket identities must be correlated before Brain can trust the data.

### Agent and Bridge may both submit with deduplication

This could appear to improve availability.

Trade-offs: ownership remains ambiguous; payload completeness, ordering, and account binding can differ; deduplication cannot reliably distinguish a legitimate replacement snapshot from a stale competing snapshot.

## Decision outcome

Choose **Agent as the sole raw-ingestion producer**.

- Agent alone sends `ingest.snapshot` and `ingest.delta` to Brain. It is the source of observation and retains unacknowledged data for retry.
- Brain alone validates and durably commits raw ingest, advances the source checkpoint, builds derived state, and publishes consumer messages. After acknowledgment, Brain is the system of record; Agent storage remains a capture cache and retry source.
- Bridge is a read-model consumer. It never reads Agent IndexedDB, sends raw-ingestion messages, proxies Brain commands, or determines Agent connectivity through `chrome.runtime`.
- Brain accepts ingestion messages only on a successfully bound Agent session. The same message types are protocol errors on a Bridge session.
- Brain sends executable commands directly to the bound Agent. Agent alone executes them and reports results. Bridge may display command state but is not a transport hop.
- Agent data is partitioned by `creator_account_id`. A snapshot is a replacement for one account and one fenced source stream, never a global cache replacement.

Reconnect, acknowledgment, sequence, and idempotency rules are defined in ADR 0004. Identity binding is defined in ADR 0003.

## Consequences

### Positive

- Snapshot and delta ownership is unambiguous and independent of UI availability.
- Bridge can run in browsers without extension privileges and has one state source: Brain.
- Brain can reject role-confused messages before they mutate state.
- Direct Brain-to-Agent commands have one delivery and audit path.

### Negative

- Agent requires a durable outbox and consistent snapshot/high-water-mark logic.
- Brain must expose an explicit resync protocol and durable ingestion acknowledgments.
- Any convenience features that read extension storage directly from Bridge must be removed or redesigned as Brain queries.

## Confirmation

- Contract tests must prove that Brain rejects `ingest.snapshot` and `ingest.delta` on Bridge sessions.
- A system test must ingest data and execute a Brain command with Bridge closed.
- Static checks must find no Bridge dependency on `chrome.runtime`, Agent IndexedDB, or raw-ingestion message constructors.
