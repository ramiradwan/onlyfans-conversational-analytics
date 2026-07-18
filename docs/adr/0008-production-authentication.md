# ADR 0008: Separate hosted customer provisioning from local runtime authentication

- Status: accepted

## Context and problem statement

Production authentication must preserve the immutable role and account binding defined by ADR 0003 while keeping conversation processing inside the creator-controlled installation. Hosted identity is useful for customer authentication and installation provisioning, but ordinary local access must not depend on a hosted round trip.

The product therefore separates two planes:

- A hosted provisioning plane authenticates customers through a dedicated external customer identity (CIAM) tenant, provisions installations, and issues signed offline-verifiable grants.
- A local runtime plane consists of Brain, Bridge, and Agent. It authenticates enrolled operators and Agent installations, issues runtime sessions and tickets, and processes conversation data.

The hosted plane never receives conversation data.

## Decision drivers

- Keep conversation capture, storage, analytics, platform credentials, and command execution inside the local installation.
- Preserve immutable role and creator-account binding on every socket.
- Allow local login and reconnect while applicable signed grants remain usable under the private contracts profile.
- Use non-exportable proof keys for installations and Agents.
- Require explicit approval for each installation and creator-account association.
- Make runtime tickets purpose-bound, account-bound, short-lived, single-use, and locally revocable.
- Fail closed when development authentication or unsafe network exposure is present in production.

## Considered options

### Use hosted identity and shared cloud state for every runtime request

Brain could redirect each Bridge login to hosted identity and use hosted session, ticket, and revocation stores for local sockets and configuration requests.

Trade-offs: hosted availability becomes a dependency for local use, the trust boundary expands, and runtime metadata gains an unnecessary path out of the installation.

### Keep all identity and grant authority local

Brain could own passwords, recovery, multi-factor authentication, organization membership, and installation authorization in addition to runtime sessions.

Trade-offs: every installation becomes an identity provider and duplicates sensitive recovery and authorization workflows.

### Use hosted signed grants with local runtime authentication

The hosted plane authenticates the customer and signs narrowly scoped grants. Brain pins the signing authority, verifies grants locally, enrolls local WebAuthn and Agent credentials, and issues runtime sessions and tickets from an atomic local store.

Trade-offs: the product needs both hosted provisioning and a secured local authentication authority, and hosted changes take effect locally through refresh or bounded expiry.

## Decision outcome

Choose **hosted customer provisioning with signed offline-verifiable grants and local Brain-issued sessions and runtime tickets**.

### Authority and data boundary

| Concern | Hosted provisioning plane | Local Brain, Bridge, and Agent |
| --- | --- | --- |
| Customer identity | Authenticates durable customer principals through a dedicated external CIAM tenant. | Stores only the issuer and subject needed to bind a local credential and verified grant. |
| Installation | Verifies the hosted onboarding flow, consumes a single-use installation claim, registers the installation public key, and issues signed grants. | Generates and retains the non-exportable installation private key and proves possession. |
| Creator-account association | Records explicit approval and signs the installation/account binding. | Detects the local account and activates only the exact approved Agent/account pair. |
| Human runtime access | Supplies signed access grants. | Authenticates the enrolled operator locally and authorizes local requests and tickets. |
| Agent runtime access | Has no runtime session or ticket authority. | Pairs the Agent key, verifies challenges, issues Agent tickets, and performs local revocation. |
| Conversation runtime | Has no ingestion, query, analytics, command, or socket role. | Captures, stores, processes, displays, and commands locally under ADR 0006. |

Hosted configuration cannot change this boundary. Brain binds to loopback by default, and Brain and Agent reject non-local ingestion, analytics, configuration, and command endpoints.

### Trust anchors and signed grants

Brain is provisioned with a pinned hosted signing trust set. Signer rotation requires a software update or a key-transition statement authenticated by an already pinned key; fetching an untrusted key set is insufficient.

Each grant is a separately signed object with its own audience, subject, identifier, validity boundary, installation binding, and signer identifier:

| Grant | Purpose |
| --- | --- |
| `installation_grant` | Binds an installation and its public key to the authenticated customer context. |
| `creator_account_binding` | Binds one approved creator account to one installation. |
| `membership_snapshot` | Carries the operator roles and allowed creator accounts for local access. |
| `license_entitlement` | Carries enabled product capabilities without conversation usage data. |

Grant lifetimes and refresh behavior use bounded lifetimes with defined offline grace periods, specified in the private contracts profile. Brain verifies each audience and subject independently, refreshes grants asynchronously with installation-key proof, and records authoritative revocation responses locally. A transient hosted failure does not revoke a grant that remains usable under that profile.

Expired entitlements never block access to existing local data.

Detailed signed-object schemas, algorithms, signer separation, and key-transition rules live in a private cross-plane contracts repository. Hosted identity and provisioning implementation details live in a private hosted control-plane repository.

### Provisioning and local enrollment

Provisioning uses the hosted onboarding flow:

1. The customer authenticates through the dedicated external CIAM tenant.
2. The hosted plane issues a single-use installation claim.
3. The installation generates a non-exportable private key in the local OS credential store and proves possession of it.
4. The hosted plane consumes the claim, registers the public key, and returns signed bootstrap grants.
5. Agent detects the locally authenticated creator account but does not activate it.
6. The customer explicitly approves the creator-account association.
7. The hosted plane signs a `creator_account_binding` for the exact installation/account tuple.
8. Brain verifies the binding, completes local Agent-key pairing, and activates that relationship.

Account detection alone grants no access. Hosted provisioning cannot issue a local `auth_ticket`, connect to a local socket, request local data, or submit a command.

### Local Bridge authentication

During provisioning, Brain verifies the signed access grants and enrolls a WebAuthn platform credential with user verification required. The private key remains in the platform authenticator. Brain stores the public credential, external issuer/subject, installation identity, and verified grant references in the local authentication store.

For local login:

1. Bridge requests a random, single-use WebAuthn challenge from Brain.
2. Brain atomically consumes the challenge and verifies the assertion, origin, relying-party identity, and user-verification flag.
3. Brain verifies the signed installation, membership, and selected account-binding grants.
4. Brain issues an opaque local Bridge session and stores only its digest.
5. Cookie-authenticated state-changing requests also require a per-session CSRF value and exact `Origin` and `Host` validation.

The session cookie is `__Host-bridge_session` with `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, and no `Domain`. The cookie does not authenticate a WebSocket; Bridge obtains a separate purpose-bound ticket for every bind or reconnect.

### Local Agent authentication

Agent creates a non-exportable signing key during pairing. Brain shows the key fingerprint and detected creator account for local confirmation, and activates the credential only after verifying the exact signed account binding.

For ongoing access, Brain issues a random single-use challenge. Agent signs a canonical request value containing the challenge, method and path, request digest, ticket purpose, `agent_installation_id`, `creator_account_id`, key identifier, and Brain audience. Brain consumes the challenge atomically and verifies the proof before issuing an Agent WebSocket or configuration ticket.

Reinstallation or key loss requires pairing again. Local pairing revocation invalidates dependent challenges, tickets, sockets, capture, and commands.

### Local sessions, tickets, and socket binding

Brain stores Bridge sessions, WebAuthn and Agent challenges, Agent pairings, purpose-bound tickets, used-ticket tombstones, verified-grant references, and local revocation state in `auth.sqlite3`. Transactions and uniqueness constraints provide atomic issue, compare-and-consume, expiry, and revocation.

Each runtime ticket is an opaque value with at least 256 random bits. Brain stores only its digest and records:

- issue and expiry state;
- the authenticated local principal and parent session or Agent key;
- exactly one purpose: `bridge-websocket`, `agent-websocket`, or `agent-config`;
- exactly one role and `creator_account_id`;
- the expected `bridge_session_id` or `agent_installation_id`; and
- applicable revocation versions and verified-grant boundaries.

Ticket consumption is one transaction that changes one unused, unexpired record to used after all bindings pass. Used records remain as tombstones through their validity window. Network failure does not make a consumed ticket reusable.

Bridge presents its ticket in `bridge.hello.payload.auth_ticket`. Agent presents its ticket in `agent.hello.payload.auth_ticket` or `AgentConfigGetRequest.auth_ticket`. Invalid tickets produce a fatal `protocol.error` with code `unauthorized` and close code 1008 when safe.

Credentials, tickets, challenges, proofs, cookies, and grants are redacted from logs, traces, metrics, errors, and referrers. Ticket expiry does not terminate an authenticated socket, but parent credential or authorization revocation does.

### Revocation and failure behavior

Local logout and credential or Agent-pairing revocation take effect immediately. Brain records the revocation atomically, invalidates dependent objects, and closes affected sockets.

Hosted authorization changes take effect after an authenticated refresh response or at the applicable signed-grant boundary. There is no hosted push channel into Brain. On authorization loss, Bridge clears account-scoped transient state and requires local reauthentication or recovery. Agent stops affected capture, configuration fetch, command execution, and outbound ingestion while preserving its partitioned durable outbox for authorized recovery.

### Production fail-closed posture

Production runtime activation fails when:

- the ADR 0007 development ticket, fixed development mapping, or a selector that enables them is present in a deployable runtime;
- pinned signing trust is missing, invalid, or replaced without an authenticated transition;
- a required installation, membership, or account-binding grant is absent, invalid, mismatched, or outside its allowed validity boundary;
- `auth.sqlite3` cannot provide durable atomic challenge and ticket consumption or revocation state;
- an installation or Agent private key is exportable or replaced by a bearer-secret fallback;
- Brain is configured for non-loopback exposure without a separately accepted deployment profile; or
- packaged assets contain a default account, automatic account selection, hosted runtime endpoint, or path that sends conversation data to the hosted plane.

An unprovisioned installation exposes only the bounded loopback provisioning surface.

### Protocol compatibility

The protocol-v1 schemas remain unchanged:

- `agent.hello.payload.auth_ticket`, `agent_installation_id`, and `requested_creator_account_id` carry the Agent ticket and comparison fields.
- `bridge.hello.payload.auth_ticket`, `bridge_session_id`, and `requested_creator_account_id` carry the Bridge ticket and comparison fields.
- `AgentConfigGetRequest.auth_ticket`, `agent_installation_id`, and `creator_account_id` carry the configuration ticket and comparison fields.
- Brain derives principal, role, purpose, authorization, parent credential, grant boundaries, and revocation state from local records.
- `protocol.error` provides fatal non-retryable unauthorized behavior.

Provisioning, grant refresh, WebAuthn, pairing, and ticket issuance use separate HTTP contracts and add no WebSocket operation.

## Consequences

### Positive

- Conversation data and runtime credentials remain inside the creator-controlled installation.
- Local login and socket reconnect do not require a hosted round trip while signed authorization remains locally valid.
- Human, installation, and Agent credentials have separate scopes and revocation paths.
- Purpose-bound tickets preserve immutable role and account binding without changing protocol v1.
- Hosted compromise does not create a runtime path to local conversation data.

### Negative

- The product requires both hosted provisioning and a secured local authentication authority.
- Hosted authorization changes are not immediate while an installation is offline.
- WebAuthn enrollment, key recovery, Agent pairing, grant refresh, and signer rotation add implementation complexity.
- Loopback is the only deployment profile covered by this decision.

## Confirmation

- Provisioning tests reject reused claims, unpinned signers, wrong grant audiences or subjects, and account activation without explicit approval.
- Local login tests cover WebAuthn verification, offline signed-grant validation, reauthentication, and recovery boundaries.
- Ticket tests reject replay, expiry, wrong purpose, wrong account, wrong installation or session identifier, and revoked parents through one atomic SQLite consumer.
- Revocation tests distinguish transient hosted unavailability from authoritative denial.
- Privacy tests prove that hosted contracts cannot carry conversation messages, fan identities, analytics, platform credentials, capture events, command results, or local runtime telemetry.
- Startup tests reject development authentication, invalid trust or grants, non-atomic authentication storage, and non-loopback exposure.
- Contract tests confirm that the protocol-v1 schemas and fixtures remain unchanged.
