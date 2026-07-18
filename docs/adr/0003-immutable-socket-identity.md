# ADR 0003: Bind immutable role and account identity to every socket

- Status: accepted

## Context and problem statement

Socket role and account identity are security boundaries. URL parameters, client payloads, defaults, and recently observed connections are claims rather than authorization. A connection must bind one validated role and creator account before it can carry domain messages, and that binding must remain immutable for the connection lifetime.

## Decision drivers

- No message may change the tenant or role of an already accepted socket.
- Account identity must be authorized, not guessed from a URL, default, or most recent connection.
- Agent identity must survive service-worker restarts; socket identity must not.
- Bridge account switching must not retain or render the prior account's state.
- Concurrent Agent writers must be fenced.

## Considered options

### Continue using role and user path parameters

This is easy to route and inspect.

Trade-offs: the path is an unverified client claim, identity changes cannot safely update subscriptions, generic role strings enlarge the attack surface, and defaults can silently cross data partitions.

### Use role-specific endpoints and a validated application handshake

The endpoint fixes the role; a first message identifies the client instance and requested account; Brain authorizes and binds the socket before accepting other messages.

Trade-off: connection establishment has an extra state and needs an authentication/ticket mechanism.

### Carry account identity on every message

This supports multiplexing one socket across accounts.

Trade-offs: every handler must revalidate routing, one mistaken field can cross tenants, and mid-stream account switching complicates ordering and fencing.

## Decision outcome

Choose **role-specific endpoints plus an immutable validated handshake**.

### Identity vocabulary

| Identifier | Meaning | Lifetime and authority |
| --- | --- | --- |
| `principal_id` | Authenticated application subject | Derived by Brain from authentication; never accepted from a payload as authority. |
| `creator_account_id` | Authorized upstream creator account and data partition | Selected by the client, validated by Brain against the principal, and immutable for one socket. This replaces ambiguous routing uses of `user_id`. |
| `platform_user_id` | A fan or other upstream user appearing in captured data/presence | Domain data only; never a socket or tenant identity. |
| `agent_installation_id` | Random UUID created once per extension installation | Persisted in `chrome.storage.local`; stable across MV3 worker restarts; not an authorization credential. |
| `bridge_session_id` | Random UUID for one Bridge page session | Stable across that page's reconnects, regenerated for a new page session; not an authorization credential. |
| `connection_id` | Brain-generated identifier for one accepted WebSocket | Changes on every reconnect and is returned by Brain; used for logs, correlation, and fencing. |

### Binding protocol

- Use fixed `/ws/agent` and `/ws/bridge` endpoints. The endpoint determines the only permitted message union; `client_type` is not client-selectable.
- Before any domain message, Agent sends `agent.hello`; Bridge sends `bridge.hello`. The hello includes protocol version, its non-secret instance/session identifier, and requested `creator_account_id`.
- Brain validates authentication, authorization, role, protocol compatibility, and account selection. Only then does it return the role-specific session acknowledgment with `connection_id` and bind the socket to the tuple.
- Payload identity fields are correlation data, not routing authority. Brain uses the binding for channels, persistence partitions, and authorization. A conflicting identity in any payload is a fatal protocol error.
- Agent must not open an ingest session until it has a real, validated creator account. There is no production `demo_user`, “latest user,” or URL-segment fallback.
- One socket binds one creator account. Supporting multiple accounts means separate bound sockets and account-partitioned local stores.

### Identity changes and competing Agents

- If Agent observes a different logged-in creator account, it first stops sending on the old binding, persists captured data in the correct account partition, closes the old socket with an identity-change reason, opens and validates a new socket, and completes resynchronization before deltas for the new account are released. It must never relabel an old socket or reuse an unpartitioned snapshot.
- When Bridge changes account selection, it closes the old socket, clears all account-scoped transient state, binds a new socket, and waits for the new `state.snapshot` before rendering account data.
- Brain permits one active Agent ingestion lease per `creator_account_id` in protocol v1. A newly authenticated and accepted Agent connection supersedes the older lease, issues a new fencing token, and causes the old socket's subsequent writes to be rejected. Multiple Bridge readers are allowed.

Authentication remains distinct from these identity semantics and Brain verification remains mandatory. ADR 0007 defines a development-only validator, and ADR 0008 defines the production authentication boundary.

## Consequences

### Positive

- An open socket cannot silently change role or tenant.
- MV3 restarts preserve installation identity while correctly creating a new connection identity.
- Logs and metrics can distinguish principal, creator account, client installation/page session, and socket.
- Active-writer fencing prevents two Agent snapshots from racing for one account.

### Negative

- Account switches require an explicit disconnect, store reset, and resync.
- Agent local persistence must be account-partitioned.
- The development ticket is deliberately non-production and is unavailable in production authentication mode.
- Superseding an older Agent favors recovery over simultaneous multi-device capture; multi-writer merge would require a later ADR.

## Confirmation

- Tests must reject all non-hello messages before binding and all wrong-role messages afterward.
- Tests must prove that a discovered account change cannot send new-account data over the old connection.
- Tests must show the old Agent is fenced after a replacement connection is accepted.
- No production configuration or routing path may default to or auto-select `demo_user`/`latest_user_id`.
