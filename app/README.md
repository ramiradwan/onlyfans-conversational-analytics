# Brain application

`app/` is the local-first Brain runtime. It serves the compiled Bridge, accepts
authenticated protocol-v2 Agent ingestion, owns canonical SQLite truth, and builds
rebuildable read models in a separate projections SQLite database.

## First-run production configuration

The shipped runtime uses `WEBSOCKET_AUTH_MODE=local_session`.
`app.core.first_run.initialize_production_configuration` provisions the
production runtime configuration. It requires `VerifiedGrantBindings` and the
packaged Chrome extension ID. `VerifiedGrantBindings` is installation identity:
the verified principal and its local Bridge role. Authorized creator accounts
are bound separately and are never written into installation configuration, so
an installation is valid with no authorized account. The initializer generates
the bootstrap token and signing secret with the operating system cryptographic
random source. It stores them, the verified bindings, and absolute SQLite paths
in `runtime.env` below the platform-standard per-user application-data
directory. Neither secret is returned or logged.

The initializer creates `runtime.env` once. A second call with identical inputs
reuses it without regenerating secrets. Different verified bindings or an
extension ID change fail closed and require an explicit reprovisioning workflow.
At startup, `app.core.config` reads this file; a source checkout may instead use
the explicitly development-only `app/.env.example` as `app/.env`.

## Local session bootstrap and browser handoff

The direct user-agent bootstrap surface accepts:

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

The launcher uses the browser handoff surface:

```http
POST /api/v1/session/handoff
Host: bridge.localhost:17871
Authorization: Bootstrap <launcher-secret>
```

The response contains one random handoff code with a 30-second default TTL and no
cookie. The system browser redeems that code once at
`GET /api/v1/session/handoff?code=...`. A successful redemption sets the sealed
session cookie and redirects to `/` with status 303. Unknown, expired, and consumed
codes have the same response. Handoff responses disable caching and referrer
transmission.

Before sending the handoff request, the Windows launcher identifies the kernel TCP
listener for port 17871 and requires the configured loopback address, Brain image
path, and current-user process SID. A matching listener is reused. An empty port
starts Brain without a console window and is inspected again before the request.
The launcher does not terminate an unrecognized owner or select another port.

| Failure code | Result |
| --- | --- |
| `configuration_unavailable` | No request is sent and the browser remains closed. |
| `port_inspection_failed` | The listener is not trusted and the browser remains closed. |
| `port_conflict` | The owner remains running; the launcher reports that port 17871 must be released. |
| `brain_start_failed` | The browser remains closed. |
| `brain_start_timeout` | The browser remains closed after the bounded ownership poll. |
| `handoff_failed` | The browser remains closed. |
| `browser_open_failed` | The launcher reports that the system browser could not be opened. |

When the durable bootstrap credential is already consumed, the verified launcher
opens `http://bridge.localhost:17871` directly so the browser can present its
existing sealed cookie. Other handoff failures remain closed.

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
