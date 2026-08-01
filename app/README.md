# Brain application

`app/` is the local-first Brain runtime. It serves the compiled Bridge, accepts
authenticated protocol-v2 Agent ingestion, owns canonical SQLite truth, and builds
rebuildable read models in a separate projections SQLite database.

## First-run production configuration

The shipped runtime uses `WEBSOCKET_AUTH_MODE=local_session`. Before importing
`app.main`, the launcher calls
`app.core.first_run.initialize_production_configuration` with
`VerifiedGrantBindings` emitted by its grant-verification step and the packaged
Chrome extension ID. The initializer generates the bootstrap token and signing
secret with the operating system cryptographic random source. It stores them,
the verified bindings, their bundle digest, and absolute SQLite paths in
`runtime.env` below the platform-standard per-user application-data directory.
Neither secret is returned or logged.

The initializer creates `runtime.env` once. A second call with identical inputs
reuses it without regenerating secrets. Different verified bindings or an
extension ID change fail closed and require an explicit reprovisioning workflow.
At startup, `app.core.config` reads this file; a source checkout may instead use
the explicitly development-only `app/.env.example` as `app/.env`.

## Local session bootstrap

The launcher sends:

```http
POST /api/v1/session/bootstrap
Host: bridge.localhost:17871
Authorization: Bootstrap <launcher-secret>
```

Brain compares the credential without placing it in a URL, atomically records its
hash as consumed in durable SQLite state, sets the signed
`__Host-bridge_session` HttpOnly/Secure/SameSite=Strict cookie, and redirects to
`/`. Reuse remains rejected after process restart. Authenticated HTML and bootstrap
responses use `Cache-Control: no-store`.

Development and tests explicitly select `development_stub`; non-development
configuration never falls back to the development account.

## Runtime boundaries

- `api/endpoints/transport_ws.py` implements Agent/Bridge protocol-v2 transport and
  authenticated Agent configuration.
- `api/endpoints/history.py` implements creator-authorized history settings and
  projection-owned REST message paging.
- `persistence/history.py` commits canonical ingestion and activates bounded read
  models.
- `persistence/sql/` is the authoritative canonical schema.
- `persistence/projection_sql/` is the independent, disposable projection schema.
- `persistence/projection_pipeline.py` is the deterministic local NLP/LPG seam.

Snapshot entities are staged in typed SQLite columns and merged with set-based
validation/upserts. Projection work is durable, processed off the event loop in
bounded batches, and activated through canonical intents. SQLite transaction
visibility keeps the previous generation readable until the replacement generation
and its durable Bridge change-log entry commit atomically.

When no local NLP model is configured, the pipeline persists an explicit
`unavailable` analysis with `unknown` sentiment; it does not invent a score or
coerce unavailable analytics to zero. Signer and passive messages enter this same
canonical projection path.

`app/main.py` mounts only the protocol-v2 transport, authenticated history API,
and frontend routes. Static or sample insight routes are not part of the runtime.
