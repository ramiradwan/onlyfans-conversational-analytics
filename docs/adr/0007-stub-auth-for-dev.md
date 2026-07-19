# ADR 0007: Use a static authentication ticket for local development

- Status: superseded by [ADR 0008](0008-production-authentication.md)

## Context and problem statement

Local development needs a deterministic credential that exercises the role, identity, authorization, and binding flow from ADR 0003 without being mistaken for production authentication. Fixed role endpoints, role-specific hello messages, Brain validation, an authorized `creator_account_id`, immutable socket binding, Brain-assigned `connection_id`, and Agent fencing remain active in development mode.

## Decision drivers

- Support deterministic local development without introducing a separate handshake.
- Keep the WebSocket protocol and handler state machine production-shaped.
- Prevent `demo_user`, URL values, or client claims from becoming identity authority again.
- Make the development-only security posture unmistakable and fail closed outside local development.
- Prevent the development validator from being enabled in a deployable production runtime.

## Considered options

### Use production authentication for all development

This would avoid a temporary validator and provide realistic security behavior from the start.

Trade-offs: local setup would depend on provisioned identity infrastructure and credentials, making isolated development and protocol testing unnecessarily difficult.

### Use a well-known static development ticket in the normal handshake

This exercises the final handshake shape and immutable binding state machine while replacing only real ticket validation with deterministic development mapping.

Trade-offs: it provides no secrecy, user authentication, tenant isolation, revocation, or deployment security. It is acceptable only under an enforced local-development boundary and must later be removed.

### Trust hello/path identity with no ticket validation

This is the smallest implementation.

Trade-offs: it bypasses the production-shaped handshake state machine and permits clients to choose their own principal/account authority.

## Decision outcome

Choose **a well-known static ticket in the production-shaped handshake for explicit local-development mode only**.

### Development binding

- Development authentication uses an explicit non-secret fixture supplied by the local Brain; the extension has no compiled fallback credential.
- When and only when Brain is in explicit local-development authentication mode, a valid occurrence of that ticket maps to:
  - `principal_id = "dev-principal"`
  - `creator_account_id = "dev-creator-account"`
- Agent and Bridge provide the ticket through the same credential slot used by the WebSocket connection/hello validation flow. No dev-only identity message, alternate socket endpoint, or validation bypass is allowed.
- `agent.hello` and `bridge.hello` still carry the identifiers and requested account defined by ADR 0003 and ADR 0006. Brain requires the requested account to equal the fixed development account and derives the principal/account binding from the validated ticket, not from the claim.
- Role-specific endpoints, protocol negotiation, `agent_installation_id`, `bridge_session_id`, Brain-assigned `connection_id`, account immutability, writer fencing, identity-change reconnect, message unions, and all failure behavior from ADRs 0003 and 0006 remain fully in force.
- A missing or different ticket is rejected. The stub does not provide wildcard access or accept arbitrary development account identifiers.

### Environment restriction

- This stub is development-only and must never be deployed outside local development.
- Brain must require an explicit development-auth mode to enable it and must fail closed when that mode is absent.
- Deployment/startup safeguards must reject the static validator in production-like modes and when the service is configured for a non-local exposure. Treating the well-known literal as a secret does not make deployment acceptable.
- Logs and UI must identify the active authentication mode as development stub so it cannot be mistaken for production authentication.

### Production restriction

Production authentication replaces this static validator and must:

- replace the static validator with a real ticket issuer/validator and authorization source;
- derive `principal_id` and authorized `creator_account_id` values from the production credential;
- define ticket acquisition, transport, expiry, revocation, rotation, replay protection, and failure behavior;
- remove the hardcoded ticket and fixed development identity mapping from deployable paths;
- preserve the role-specific hello shapes, identity vocabulary, immutable binding, connection identity, fencing, and reconnect rules exercised in local development; and
- replace stub-specific tests with production authentication, authorization, and negative-path tests.

Only the credential issuance/validation and principal-to-account authorization steps are temporary. The communication protocol surface is not.

## Consequences

### Positive

- Implementation can proceed with deterministic Agent and Bridge handshakes.
- Development exercises the same bound-session and wrong-role/account rejection paths expected in production.
- No temporary `demo_user` routing or client-authoritative identity is introduced.
- Production authentication can replace one validation boundary without redesigning WebSocket message types.

### Negative

- The static ticket authenticates nobody and isolates no real tenants.
- Anyone who can reach a development Brain can know and present the ticket; network locality and startup guards are essential.
- Tests using the fixed identities do not validate real credential lifecycle, authorization, revocation, or replay behavior.
- Production startup is blocked if the stub is active or selectable.

## Confirmation

- In development-auth mode, Agent and Bridge using the exact ticket and fixed account complete the normal ADR 0003 handshake and receive distinct Brain-generated connection identifiers.
- Missing, incorrect, wrong-account, pre-handshake, and wrong-role inputs are rejected.
- The static ticket cannot enable binding when development-auth mode is off.
- Startup/deployment tests reject stub authentication with production-like or non-local exposure settings.
- Production startup checks must verify that the static validator is not active or selectable.
