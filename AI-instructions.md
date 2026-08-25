# AI contributor instructions

Use [CONTRIBUTING.md](CONTRIBUTING.md) for setup and contribution rules, [docs/testing.md](docs/testing.md) for tests, and [docs/style.md](docs/style.md) for documentation.

Accepted [architecture decisions](docs/adr/README.md) are authoritative. Read the relevant ADR before changing system boundaries, persistence, authentication, protocol behavior, or packaging. If implementation and an accepted ADR disagree, do not silently choose one; correct the inconsistency or update the architecture decision.

## Architecture constraints

- Conversation content stays on the creator-controlled machine. Hosted services may provision identity or signed grants but do not receive conversation content.
- **Agent** is the only raw-ingestion producer. Its delivery state is durable and account-scoped.
- **Brain** is the loopback local service and owns authoritative local state. `auth.sqlite3` and `canonical.sqlite3` are authoritative; projection databases are rebuildable.
- Brain runs with one application writer. Adding another writer or process requires an architecture decision.
- **Bridge** consumes Brain-owned state. It must not read Agent storage or act as an ingestion or command proxy.
- Keep Bridge WebSocket state bounded. Historical message bodies use authenticated REST paging instead of full-history socket snapshots.
- Protocol v2 is the supported protocol. Shared fixtures under `shared/fixtures/protocol/v2` must agree across Python, Agent, and Bridge. Do not add v1 fallback behavior.
- Raw platform response bodies, cookies, signing rules or headers, and raw upstream cursors stay inside Agent and must not appear in logs or diagnostics.
- The extension must not add `webRequestBlocking`, cookie access, debugger access, native messaging, remote executable code, or unexpected origins without an explicit architecture and security change.
- Analytics and enrichment are derived state. Partial coverage, projection failure, and delayed live state must remain visible instead of being presented as complete data.

For the production topology and persistence boundary, read [ADR 0009](docs/adr/0009-local-first-topology-and-persistence.md). For signer-backed history acquisition and protocol v2, read [ADR 0010](docs/adr/0010-signer-history-acquisition-and-bounded-state.md).

## Editing rules

- Preserve unrelated work in the checkout.
- Do not edit generated assets, generated contracts, or generated types by hand. Use the owning generator or build step.
- Keep secrets, conversation text, unnecessary user identifiers, authentication material, and raw protocol frames out of logs and fixtures.
- Use the existing component boundaries: `app/` for Brain, `extension/` for Agent, and `frontend/` for Bridge.
- Update or supersede an ADR when a change alters an accepted architecture decision.
- Prefer the existing canonical specification over copying the same rule into another file. See the [documentation index](docs/README.md).

## Verification

Run the checks that cover the change and use CI as the full matrix. See [docs/testing.md](docs/testing.md).

For protocol changes, verify the shared protocol fixtures in all affected components. For extension permission or packaging changes, run the extension build and audit. For documentation changes, follow [docs/style.md](docs/style.md).
