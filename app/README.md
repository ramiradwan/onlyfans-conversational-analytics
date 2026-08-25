<!-- CODE-VERIFY: Verify runtime boundaries, module paths, persistence ownership, and security claims against source before editing. -->

# Brain

Brain is the local backend for OnlyFans Conversational Analytics. It runs on the creator's computer, accepts authenticated Agent and Bridge traffic, stores canonical conversation data, builds derived analytics, and serves Bridge.

## Responsibilities

- Authenticate local Agent and Bridge sessions.
- Validate API and protocol input before changing application state.
- Store authoritative local conversation and authentication data.
- Build and recover derived projections and analytics.
- Serve the compiled Bridge interface over the local runtime.

## Data and process boundaries

`auth.sqlite3` and `canonical.sqlite3` hold authoritative local state. Projection databases are derived and can be rebuilt from canonical data.

Brain runs as one application writer. Changes that add another writer or move conversation content to a hosted service require an architecture decision.

See [ADR 0009](../docs/adr/0009-local-first-topology-and-persistence.md) for the production topology and persistence boundary.

## Main areas

- `api/endpoints/` — HTTP and WebSocket boundaries.
- `persistence/` — canonical storage, migrations, and projection activation.
- `services/` — application workflows.
- `analytics/` — derived analytics and graph projections.
- `security/` — local authentication and runtime security checks.
- `provisioning/` — first-run provisioning surface.

## Related documentation

- [Brain API endpoints](api/endpoints/README.md)
- [Analytics](analytics/README.md)
- [Communication overview](../communication-spec.md)
- [Production authentication](../docs/adr/0008-production-authentication.md)
- [Testing](../docs/testing.md)
