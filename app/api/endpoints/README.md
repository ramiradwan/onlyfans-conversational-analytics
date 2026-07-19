# Brain API endpoints

These FastAPI route modules expose the loopback-only Brain boundary. Account authority comes from the authenticated session or bound WebSocket, never from an arbitrary client-selected identifier.

## `transport_ws.py`

- `/ws/agent` accepts the strict protocol-v2 Agent hello, sequenced deltas, bounded snapshot frames, presence, configuration acknowledgements, and command results.
- `/ws/bridge` accepts the strict protocol-v2 Bridge hello and state-resynchronization request.
- `GET /api/v1/agent/config` returns the authenticated immutable Agent-config-v2 document with ETag validation. The purpose-bound ticket is accepted only in the Authorization header.

Socket role, account, installation, stream, connection, and fencing identity are validated before domain writes. Wrong-role, pre-handshake, unsupported-version, identity-conflicting, stale-fence, and unauthorized messages fail closed with safe protocol errors.

## `history.py`

- `POST /api/v1/agent/pairing` issues one short-lived, exact-account Agent pairing ticket to an authenticated creator.
- `GET /api/v1/conversations/{conversation_id}/messages` returns authenticated, projection-generation-bound message pages with HMAC cursors.
- `GET /api/v1/settings/history` exposes desired/effective local history state to authorized roles.
- `PUT /api/v1/settings/history` and `DELETE /api/v1/settings/history/consent` require creator authority, same-origin CSRF protection, and optimistic `If-Match`.

Message and settings responses are `no-store`. Local page exhaustion is distinct from proven upstream history coverage.

## `frontend.py`

- `POST /api/v1/session/bootstrap` consumes a launcher secret from the Authorization header once, establishes the exact local account/role/platform binding, and redirects without placing credentials in a URL.
- `GET /` serves the compiled Bridge assets and injects only the account-scoped runtime values needed by the frontend.

## Responsibilities

Endpoints validate transport syntax, authentication, authorization, origin, and CSRF requirements, then delegate to the canonical persistence/service boundary. Canonical merge, sequencing, coverage derivation, checkpoint advancement, projection work, and activation are never implemented as route-local state.

Bridge analytics and read models come from canonical protocol-v2 state and the authenticated history API. No separate sample-insight or schema route is mounted.
