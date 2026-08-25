<!-- CODE-VERIFY: Verify route paths, authentication rules, response behavior, and mounted endpoint modules against source before editing. -->

# Brain API endpoints

These FastAPI route modules expose Brain over the local runtime boundary. Account authority comes from the authenticated session or bound WebSocket, not from an arbitrary client-selected identifier.

## `transport_ws.py`

- `/ws/agent` accepts protocol-v2 Agent sessions, ingestion, presence, configuration acknowledgements, and command results.
- `/ws/bridge` accepts protocol-v2 Bridge sessions and state-resynchronization requests.
- `GET /api/v1/agent/config` returns the authenticated Agent configuration document with ETag support. Its purpose-bound ticket is accepted only in the Authorization header.

Socket role, account, installation, stream, connection, and fencing identity are checked before domain writes. Wrong-role, pre-handshake, unsupported-version, identity-conflicting, stale-fence, and unauthorized messages fail with bounded protocol errors.

## `history.py`

- `POST /api/v1/agent/pairing` issues one short-lived, account-bound Agent pairing ticket to an authenticated creator.
- `GET /api/v1/conversations/{conversation_id}/messages` returns authenticated message pages bound to a projection generation and signed cursor.
- `GET /api/v1/settings/history` returns the local history state for the authenticated account.
- `PUT /api/v1/settings/history` and `DELETE /api/v1/settings/history/consent` require creator authority, same-origin CSRF protection, and `If-Match`.

Message and settings responses disable caching. Local page exhaustion is distinct from proven upstream history coverage.

## `insights.py`

`/api/v1/insights` exposes authenticated analytics reads from the active projection. Requested account identifiers are checked against the authenticated session, and unavailable or invalid projection state is returned explicitly instead of being replaced with sample data.

## `webauthn.py`

`/api/v1/webauthn` provides activation-gated, same-origin WebAuthn registration and login ceremonies. A successful login finish creates the sealed local Bridge session cookie with `Secure`, `HttpOnly`, and `SameSite=Strict` attributes.

## `frontend.py`

- `POST /api/v1/session/bootstrap` consumes a launcher bootstrap credential once and redirects to Bridge. It does not create the browser session cookie.
- `POST /api/v1/session/handoff` consumes a launcher bootstrap credential and returns one short-lived handoff code without setting a cookie.
- `GET /api/v1/session/handoff` consumes one valid handoff code and redirects to Bridge. Browser authentication is completed through the WebAuthn flow.
- `GET /` serves the compiled Bridge assets and injects the account-scoped runtime values required by the frontend.

## Responsibilities

Endpoints validate transport syntax, authentication, authorization, origin, and CSRF requirements, then delegate state changes and reads to service and persistence boundaries. Canonical merge, sequencing, projection work, and activation do not live in route-local state.

See [Brain](../../README.md) for the runtime overview and [Communication overview](../../../communication-spec.md) for protocol behavior.
