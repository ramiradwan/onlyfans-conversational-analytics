# Project guide — OnlyFans Conversational Analytics

This is the implementation guide for contributors and code-generation tools. Accepted architecture decisions in [`docs/adr/`](docs/adr/README.md) are normative. In particular, [ADR 0009](docs/adr/0009-local-first-topology-and-persistence.md) defines the production runtime and persistence topology, and [ADR 0010](docs/adr/0010-signer-history-acquisition-and-bounded-state.md) defines signer-backed history acquisition and protocol v2. If this guide differs from an ADR, correct this guide.

## Product purpose

The product is a local-first conversation analytics system:

- Agent is a Chrome MV3 extension that captures creator-visible activity and may acquire consented historical pages through a bundled read-only signer.
- Brain is one loopback-only FastAPI/Pydantic process. It authenticates Agent and Bridge, validates protocol messages, owns canonical SQLite truth, coordinates projections, runs enrichment/analytics, and serves the compiled frontend.
- Bridge is a React/Vite application. It consumes Brain-owned summary state and authenticated REST pages; it never reads Agent storage or acts as an ingestion/command proxy.

NLP enrichment, therapy-research-style labeled property graph (LPG) modeling, and analytics remain product goals. Their results are rebuildable projections. Cosmos DB, Redis, an external broker, or any hosted conversation-data service is not part of the authoritative production path.

## Architecture invariants

1. Conversation data remains on the creator-controlled machine. Hosted services may provision identity/grants but do not receive conversation content.
2. `auth.sqlite3` and `canonical.sqlite3` are authoritative. `projections.sqlite3` is rebuildable. No correctness rule depends on a cross-file transaction.
3. Brain runs as one application worker and publishes post-commit changes in process. A second writer/process requires a new architecture decision.
4. Agent is the only raw-ingestion producer. Delivery is an account-scoped durable outbox with contiguous source sequence, idempotency, fencing, acknowledgements, and explicit repair.
5. Canonical chats and messages are keyed by `(creator_account_id, platform_entity_id)`. Installation, stream, event, sequence, and passive/signer origin are provenance only.
6. Protocol `"2"`, Agent-config schema `"2"`, extension semver, signer semver/signing schema, IndexedDB version, and SQLite schema versions are independent.
7. Bridge WebSocket state is bounded: conversation summaries, one-message previews, analytics, coverage, projection, and freshness. Historical message bodies always use authenticated REST paging.
8. Acquisition coverage, projection readiness, and live freshness are independent. Brain derives completeness from typed evidence; Agent never supplies a trusted completion flag.
9. Raw platform response bodies, cookies, signing rules/headers, and raw upstream cursors never leave Agent and never appear in logs or diagnostics.

## Repository map

### `app/` — Brain

- `app/main.py`: constructs FastAPI and registers HTTP/WebSocket/frontend routes.
- `app/protocol/`: Pydantic v2 protocol/config contracts and directional unions.
- `app/api/endpoints/`: transport, settings, message-page, insight, schema, and frontend endpoints.
- `app/transport/`: connection/session manager and ingest routing.
- `app/persistence/`: repository interfaces, SQLite databases/migrations, canonical history, projection generation/read models.
- `app/services/`: configuration, ingest, command, enrichment, graph, and analytics workflows.
- `app/models/`: domain and response models.

Network endpoints validate with Pydantic and delegate business logic. Services do not construct HTTP responses. Canonical mutation, deduplication, checkpoint advancement, coverage transition, revision allocation, and projection work are committed together in `canonical.sqlite3`. Projection workers consume durable work after commit.

### `extension/` — Agent

- `background.js`: composes the runtime and bundled signer; it contains no development account fallback.
- `transport/durable-outbox.mjs`: account-scoped entities, deterministic merge, atomic page commit, outbox, jobs, and bounded snapshot construction.
- `transport/indexeddb-ingestion-storage.mjs`: one IndexedDB database per Brain-authorized account. Only installation ID may use global Chrome persistent storage.
- `transport/history-coordinator.mjs`: consent/session/identity gates and cross-page scheduling.
- `transport/agent-websocket.mjs`: protocol-v2 session/fence, delta/snapshot replay, progress acknowledgement, and restart recovery.
- `protocol/`: dependency-free v2 validation aligned with Python/shared fixtures.
- `build.mjs`: deterministic, lockfile-pinned MV3 bundle and permission/remote-code audit.

The signer validates one page and returns typed items, opaque continuation, and boundary evidence. Agent advances the continuation only after one atomic `commitPage`. `webRequest` is observation-only; do not add `webRequestBlocking`, cookies, debugger, native messaging, remote code, or unexpected origins.

### `frontend/` — Bridge

- React, TypeScript, Vite, MUI, and Zustand are the supported stack.
- `src/protocol/` validates Brain-owned v2 messages.
- `src/store/transportStore.ts` owns account-scoped summary/readiness state and bounded page caches.
- `src/services/messageApi.ts` and `historySettingsApi.ts` own authenticated REST access.
- `src/views/SettingsView.tsx` hosts history consent/controls; all roles may view, creators mutate.
- Dashboard, Inbox, Analytics, and AppBar must represent partial coverage, projection failure, and delayed live state truthfully.

Do not reintroduce complete message arrays into WebSocket snapshots or clone all history into Zustand. Keep page caches and rendered DOM bounded; preserve scroll anchor/focus on prepend.

## Coding conventions

- Python 3.10+, FastAPI, Pydantic v2, explicit types, absolute imports, small purpose-driven functions.
- JavaScript/TypeScript uses exact runtime validation at trust boundaries and deterministic normalized material for equality.
- REST errors use typed HTTP status/details; WebSocket failures use the protocol union and safe details.
- Never log conversation text, user identifiers where unnecessary, auth tickets, cookies, signer rules, upstream cursors, raw bodies, or frames.
- Preserve unrelated dirty work. Generated assets/types are changed only through their owning generator.

## NLP, LPG, and analytics

Canonical messages feed the same enrichment path regardless of passive or signer origin. Sentiment, topics, embeddings, engagement classification, graph nodes/edges, rankings, and aggregates are projection data. They must be deterministic from canonical truth or carry an explicit model/build version and canonical high-water mark.

Partial metrics carry `basis`, observed/complete range, sample size, as-of time, and projection revision. Partial additive counts are lower bounds. Ratios/averages/rankings may describe only the synchronized subset. Lifetime/exact and trend claims require complete compatible coverage. Projection failure renders unavailable, never sample/static fallback.

## Contract and security verification

- Shared fixtures under `shared/fixtures/protocol/v2` must validate identically in Python, Agent, and Bridge. Do not add v1 compatibility directories or fallback selection.
- Run deterministic merge permutations against Agent and Brain.
- Fault-inject every page-commit write and snapshot stage/commit boundary.
- Test worker/Brain restart, account switching, stream resets, multiple installations, stale cursors, CSRF/authorization, and projection activation.
- Qualify snapshots at 10,000 messages in CI and 100,000 messages for Beta, proving every frame below 512 KiB and memory proportional to one frame.
- Audit the built extension for pinned signer code, observation-only permissions, no remote code, and read-only network methods.
- Verify Settings/Inbox keyboard and screen-reader behavior, visible focus, text-plus-icon status, stable prepend, bounded cache/DOM, and no serious/critical automated accessibility findings.

## Common commands

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
```

The communication details live in [`communication-spec.md`](communication-spec.md); frontend behavior and design tokens live in [`frontend/frontend-design-spec.md`](frontend/frontend-design-spec.md).
