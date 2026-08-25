<!-- CODE-VERIFY: Verify framework dependencies, source layout, transport behavior, build scripts, and build output against frontend source and configuration before editing. -->

# Bridge frontend

Bridge is the browser interface for OnlyFans Conversational Analytics. It reads Brain-owned state through authenticated WebSocket and REST interfaces and does not read Agent storage directly.

## Responsibilities

- Present conversations, analytics, settings, and runtime status from Brain-owned data.
- Keep account-scoped UI state bounded instead of loading complete message history into memory.
- Load historical messages through authenticated REST paging.
- Represent partial data, unavailable projections, and stale live state without presenting them as complete.
- Follow the frontend design and accessibility contracts.

## Stack

Bridge uses React, TypeScript, Vite, MUI, and Zustand. The exact dependency versions are defined in `package.json` and the lockfile.

## Main areas

- `src/protocol/` — validation for Brain protocol messages.
- `src/store/` — account-scoped frontend state.
- `src/services/` — WebSocket and REST access.
- `src/views/` — routed application views.
- `src/components/` — reusable interface components.
- `src/routing/` — route and navigation setup.
- `src/theme/` — design tokens and generated theme output.

## Develop

From the repository root:

```powershell
npm ci --prefix frontend
npm run dev --prefix frontend
```

The Vite development server runs on port `5173` and proxies API and WebSocket requests to the local Brain development server.

## Build

```powershell
npm run build --prefix frontend
```

The production build writes Bridge assets to `app/static/dist`, where Brain serves them.

For the full test matrix, see [Test changes](../docs/testing.md).

## Related documentation

- [Bridge frontend design](frontend-design-spec.md)
- [Communication overview](../communication-spec.md)
- [Brain](../app/README.md)
