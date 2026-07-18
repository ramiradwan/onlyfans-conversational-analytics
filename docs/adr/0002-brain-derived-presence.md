# ADR 0002: Make Brain the authority for derived presence

- Status: accepted

## Context and problem statement

Platform-user presence, Agent connectivity, and Bridge connectivity are different states. Presence observations become stale, MV3 workers can stop without a clean shutdown, and socket availability does not establish whether a platform user is online. The system therefore needs explicit freshness semantics and one consumer-facing authority.

## Decision drivers

- Distinguish upstream-user presence from Agent connectivity and Bridge connectivity.
- Never show indefinitely stale users as online.
- Do not turn lack of observation into a false assertion that every user is offline.
- Survive Agent socket loss and MV3 worker termination without a special “clean shutdown.”
- Give every Bridge consumer the same presence view.

## Considered options

### Relay Agent presence unchanged

This is simple and has low latency.

Trade-offs: stale lists never expire, client timestamps become authoritative, late messages can move state backward, and every Bridge must invent disconnect behavior.

### Brain derives presence from expiring Agent observations

Agent reports observations; Brain orders them, applies a freshness window, and publishes the canonical current/unknown state.

Trade-off: Brain needs shared ephemeral state, expiry scheduling, and explicit freshness fields.

### Brain polls the upstream platform directly

This could make Brain independent of Agent liveness.

Trade-offs: it duplicates authenticated platform access, changes Brain's security boundary, and may be impossible or undesirable for the upstream API.

## Decision outcome

Choose **Brain-derived presence from expiring Agent observations**.

Three states are separate and must never be inferred from one another:

1. **Platform-user presence**: Agent is the sole observer and sends `presence.observed` containing a complete replacement list for one `creator_account_id`, an observation identifier, and observation time. If the upstream source is incremental, Agent must normalize it to a complete list before reporting.
2. **Agent connection state**: Brain derives `connected`, `stale`, or `disconnected` from the currently fenced Agent socket and its heartbeat lease. Bridge receives this as `agent.state`.
3. **Bridge connection state**: each Bridge determines this locally from its own Brain socket. It is not platform presence and is not broadcast as Agent state.

Brain is the authority for the consumer-facing `presence.state`:

- Brain accepts observations only from the active Agent lease for the same account, rejects out-of-order observation identifiers, records server receipt time, and assigns a configured expiry.
- A fresh explicit empty list means “known current, nobody online.” Silence or socket loss does not mean that. When the freshness lease expires, Brain publishes `freshness: unknown`, an empty authoritative list, and the last observation metadata separately so consumers cannot mistake old data for current data.
- Presence observations are ephemeral and are not replayed from the ingestion outbox. After reconnect, Agent reports only a genuinely fresh current observation. An old cached observation retains its old time and cannot renew presence.
- Brain immediately sends the current `presence.state` and `agent.state` to every newly accepted Bridge session, then sends changes. Shared Brain state must work across replicas.
- Bridge replaces its presence view from Brain messages and must gate all “online” rendering on `freshness: current`.

MV3 termination is expected. Agent heartbeats and JavaScript intervals may improve liveness but are not correctness mechanisms. If the worker disappears without a final message, Brain's leases expire. When a page event or another wake-capable extension event restarts the worker, Agent reconnects and re-establishes state; until a new observation arrives, platform presence remains unknown.

The exact heartbeat interval and presence TTL are operational configuration. Brain must enforce bounds and the TTL must exceed the expected observation cadence.

## Consequences

### Positive

- Stale online lists clear predictably without pretending that an unobserved user is offline.
- All Bridge sessions see the same account-scoped presence and Agent connectivity.
- Unexpected MV3 suspension and unclean socket loss have defined outcomes.
- Bridge no longer needs direct extension detection.

### Negative

- Presence may temporarily show unknown during reconnect even if users remain online.
- Brain needs lease expiry and a shared presence store.
- Agent must normalize multi-tab or incremental upstream events into one account-scoped replacement observation.

## Confirmation

- Tests must distinguish an explicit fresh empty observation from TTL expiry.
- Tests must show `connected -> stale/disconnected` and `current -> unknown` without a clean Agent close.
- A delayed or duplicate observation must not renew or roll back presence.
- A Bridge reconnect must receive initial presence and Agent state without waiting for the next upstream event.
