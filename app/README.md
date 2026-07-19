# Brain application

`app/` is the local-first Brain runtime. It serves the compiled Bridge, accepts
authenticated protocol-v2 Agent ingestion, owns canonical SQLite truth, and builds
rebuildable read models in a separate projections SQLite database.

## Local session bootstrap

The shipped runtime uses `WEBSOCKET_AUTH_MODE=local_session`. The launcher supplies
a random `LOCAL_SESSION_BOOTSTRAP_TOKEN` of at least 32 characters and exact
`LOCAL_PRINCIPAL_ID`, `LOCAL_CREATOR_ACCOUNT_ID`, and independently verified
`LOCAL_PLATFORM_CREATOR_ID` values.

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
