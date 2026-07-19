# OnlyFans Conversational Analytics

OnlyFans Conversational Analytics is a local-first system that captures creator-visible conversation activity, processes it into structured analytics, and presents conversation, engagement, response-time, topic, and sentiment views.

> [!IMPORTANT]
> This project is an independent tool for creators. It is not affiliated with, endorsed by, or sponsored by OnlyFans or its operator. The OnlyFans trademark is used only to describe the project's compatibility and purpose.

## Components

- **Agent** — an MV3 browser extension that captures conversation data available to the logged-in creator, maintains a durable local outbox, and executes only explicitly allow-listed actions authorized through Brain.
- **Brain** — a FastAPI backend that authenticates Agent and Bridge connections, validates and persists ingestion, derives analytics and presence state, coordinates commands, and serves the local API.
- **Bridge** — a React dashboard that consumes Brain-owned snapshots and revisioned updates. It does not read Agent storage or act as an Agent transport.

Conversation processing stays in the creator-controlled local runtime. External provisioning may issue signed offline-verifiable grants, but it does not receive conversation data.

## Architecture

- [Architecture decision records](docs/adr/README.md)
- [Communication specification](communication-spec.md)
- [Frontend design specification](frontend/frontend-design-spec.md)
- [Extension documentation](extension/README.md)

## Verification

```powershell
./.venv/Scripts/python -m pytest
cd frontend
npm run typecheck
npm run lint
npm test
npm run build
cd ../extension
npm test
npm run build
npm run audit
npm run qualify:snapshot:ci
```

The 100,000-message qualification (`npm run qualify:snapshot`) and one explicitly consented, sanitized live read-only pagination run are additional Beta gates. Deterministic checks alone do not authorize a Beta declaration.
