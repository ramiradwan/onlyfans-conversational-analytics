# ADR 0005: Make Brain the versioned Agent-configuration authority

- Status: accepted

## Context and problem statement

Agent needs a configuration source that remains safe during temporary Brain unavailability and exposes truthful required-versus-applied state. Configuration rollout, protocol compatibility, extension releases, and local storage migrations are independent version domains and must not trigger one another implicitly.

## Decision drivers

- One authoritative source for the required Agent configuration.
- Agent must operate safely through transient Brain unavailability.
- Brain and Bridge need truthful required-versus-applied drift state.
- Configuration payload delivery must be cacheable, validated, and independently evolvable from WebSocket schemas and IndexedDB migrations.
- Config changes must reach running Agents without putting large payloads on the event socket.

## Considered options

### Bundle all configuration with Agent releases

This is simple, works offline, and is strongly coupled to tested code.

Trade-offs: changing capture patterns requires an extension release; Brain cannot coordinate rollout or report drift.

### Versioned REST configuration with a WebSocket version signal

Brain serves immutable config documents over an authenticated HTTP endpoint; the socket carries only required/applied revision state and change notifications. The selected deployment profile supplies the transport security boundary.

Trade-off: this requires an authenticated endpoint, caching rules, local last-known-good storage, and an application acknowledgment.

### Push full configuration over WebSocket

This can update a running Agent immediately.

Trade-offs: it mixes control data with the event protocol, complicates reconnect and caching, and makes validation/fallback harder.

## Decision outcome

Choose **versioned REST configuration with WebSocket version signaling**.

- Brain owns a real `GET /api/v1/agent/config` endpoint. Selection is based on the authenticated Agent context and bound `creator_account_id`/release channel, not a hard-coded host.
- Every immutable document has a `config_revision` and content digest. The endpoint supports conditional fetch with ETag and never serves different content under the same revision/digest.
- Agent ships a minimal safe bundled configuration and persists the last validated good document. On startup it loads the last good document, compares it with Brain's `required_config_revision` from `agent.session`, and conditionally fetches when absent or different.
- Brain can send `config.available` when the required revision changes during a session. That message is a signal to fetch; it is not the configuration payload.
- Agent validates schema, supported capabilities, digest, and safe patterns before atomic activation. Existing tabs are reinitialized or reloaded as required by the capture design. Only after the service worker and relevant page hooks use the same revision does Agent send `config.applied`.
- Agent includes `applied_config_revision` in every `agent.hello` and `agent.heartbeat`. This makes a lost `config.applied` self-healing.
- Brain records required and applied revisions in shared Agent state and publishes them to Bridge through `agent.state`. Configuration content is not sent to Bridge. A Bridge `connection_ack`/`bridge.session` describes the Bridge connection only.
- Fetch failure keeps the last known good configuration, reports `degraded` with the failure, and retries with bounded backoff. With no valid cached config, Agent uses only the bundled safe behavior and must not claim the required revision is applied.
- Remote `config_revision`, extension release version, communication `protocol_version`, and local `storage_schema_version` are distinct fields. A config revision change never triggers a database migration by itself; migrations run only for storage schema changes.

## Consequences

### Positive

- The currently required and actually applied configuration are observable and eventually consistent.
- Agent can survive temporary Brain outages without silently discarding its last validated configuration.
- Bridge receives useful drift/health state without becoming a configuration consumer.
- Config rollout, protocol compatibility, extension release, and storage migration can evolve independently.

### Negative

- Brain needs a configuration registry, selection policy, and authenticated endpoint.
- Agent needs atomic activation and tab reinitialization behavior.
- A stale last-known-good configuration may run temporarily; UI and operations must expose the degraded state.

## Confirmation

- The endpoint must return the same digest for a revision and honor conditional requests.
- Tests must cover no cache/offline, valid cache/offline, required revision change, invalid digest/schema, and interrupted activation.
- Bridge must receive required and applied revisions through `agent.state` even when it connects after Agent.
- Changing only remote config must not run an IndexedDB migration.
