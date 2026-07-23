# Frontend — OnlyFans Conversational Analytics (“Bridge”)  
  
React 19 + MUI v9 + Vite application served via **FastAPI**, implementing the finalized    
[**frontend-design-spec.md**](frontend-design-spec.md) and [**communication-spec.md**](/communication-spec.md).  
  
Implements:  
  
- **Persistent `AppShell` layout** with MUI v9 CSS variable theming  
- **Role-based routing** (Creator vs Operator) via `useAppRoutes()` + `<Outlet />`  
- **Snapshot‑then‑delta** state hydration across multiple Zustand stores  
- **3‑tier design token pipeline** with generated MUI theme  
- **WebSocket service** for real-time updates with REST bootstrap fallback  
- **Strict type safety** for WS, REST, theme, and config  
  
---  
  
## 📖 Overview  
  
| Feature | Implementation |  
| --- | --- |  
| **Framework** | React 19, MUI v9 (CSS variables enabled), Vite |  
| **Served by** | FastAPI — Jinja injects `FASTAPI_CONFIG` into `index.html` |  
| **Data Sources** | **WebSocket** — Event-driven updates from backend<br>**REST** — Bootstrap snapshot (dev mode) & analytics endpoints |  
| **State Management** | Multiple [Zustand](https://github.com/pmndrs/zustand) domain stores: `chatStore`, `analyticsStore`, `enrichmentStore`, `systemStore`, `userStore` |  
| **Type Safety** | Auto-generated WS types (`backend-wss.ts`) from `/api/v1/schemas/wss`<br>Auto-generated REST types (`backend.ts`) from backend OpenAPI spec<br>Theme augmentation via `mui.d.ts` for token-aware MUI typing |  
| **Roles** | `"creator-ceo"` → Dashboard-first<br>`"operator"` → Inbox-first |  
  
---  
  
## 📂 Structure  
  
```plaintext  
src/  
├── main.tsx                # Mounts <App /> into #root  
├── App.tsx                  # Theme + Router + WS bootstrap  
  
├── layouts/  
│   ├── AppShell.tsx         # Persistent AppBar + Drawer + <Outlet />  
│   ├── AppAppBar.tsx  
│   └── AppDrawer.tsx  
  
├── routing/  
│   ├── AppRouter.tsx        # Wraps AppShell, loads role-based routes  
│   └── useAppRoutes.tsx     # Generates <RouteObject[]> from usePermissions()  
  
├── hooks/  
│   └── usePermissions.ts    # Role booleans & view access rules  
  
├── theme/  
│   ├── tokens.json          # Tier 1 token source of truth  
│   ├── generate-theme.ts    # Build script for theme from tokens.json  
│   ├── generated/  
│   │   ├── theme.ts         # Generated MUI theme (Tier 2 + Tier 3)  
│   │   └── tokens.ts        # Generated token constants  
│   └── index.ts             # Barrel export for theme & tokens  
  
├── store/  
│   ├── chatStore.ts         # Conversations & messages  
│   ├── analyticsStore.ts    # KPI / metrics  
│   ├── enrichmentStore.ts   # Fan360 data  
│   ├── systemStore.ts       # Connection status & presence  
│   └── userStore.ts         # Role & identity  
  
├── services/  
│   ├── websocketService.ts  # WS lifecycle + event routing  
│   └── extensionService.ts  # Chrome MV3 agent messaging  
  
├── types/  
│   ├── backend-wss.ts       # Generated WS types from JSON Schema  
│   ├── backend.ts           # Generated REST types from OpenAPI  
│   ├── config.ts            # App config typing  
│   └── mui.d.ts             # MUI Theme augmentation w/ tokens  
  
├── views/                   # Role-specific routed pages  
│   ├── CreatorDashboardView.tsx  # Creator KPIs + insights  
│   ├── AnalyticsView.tsx         # Creator detailed analytics  
│   ├── OperatorInboxView.tsx     # Operator conversation console  
│   └── GraphExplorerView.tsx     # Creator experimental graph queries  
  
├── components/  
│   ├── KpiCard.tsx  
│   ├── KpiCardSkeleton.tsx  
│   ├── MessageBubble.tsx  
│   ├── QueryInput.tsx  
│   ├── QueryResponseBubble.tsx  
│   ├── UserQueryBubble.tsx  
│   ├── ThemeToggle.tsx  
│   ├── ui/  
│   │   ├── AsyncContent.tsx  
│   │   └── Panel.tsx  
│   └── placeholders/        # Loading / empty state components  
│       └── index.tsx  
  
├── common/  
│   └── GlobalLoader.tsx     # Full-screen loading overlay  
  
├── config/  
│   ├── endpoints.ts  
│   └── fastapiConfig.ts  
  
└── utils/  
    └── index.ts  
```  
  
---  
  
## 🏛 Architecture & Flow  
  
### Persistent Layout (`AppShell`)  
- **Top AppBar** (`AppAppBar`) — Search, filters, connection indicators  
- **Side Drawer** (`AppDrawer`) — Primary navigation  
- **Main Content** — `background.default`, tokenised padding, `<Outlet />` renders active view  
  
### Role-Based Routing  
- `usePermissions()` reads `userStore.role` and returns booleans  
- `useAppRoutes()` returns `<RouteObject[]>` per role  
  - Creator → `/` = `<CreatorDashboardView />`  
  - Operator → `/` = `<Navigate to="/inbox" />`  
- `AppRouter` mounts these inside `<AppShell>`  
  
---  
  
## 📄 Views  
  
### **CreatorDashboardView**  
- **Audience:** Creator role only    
- **Purpose:** High-level KPIs + top insights at a glance  
- **Data Sources:**    
  - `useAnalyticsStore` → `topics`, `sentimentTrend`, `responseTimeMetrics`, `unreadCounts`  
  - Types: `SentimentTrendPoint`, `TopicMetricsResponse`  
- **Key UI:** KPI cards, sentiment line chart, top topics bar chart  
- **Loading States:** `KpiPlaceholder`, `ChartPlaceholder`  
  
### **AnalyticsView**  
- **Audience:** Creator role only    
- **Purpose:** Detailed analytics with tabular + chart views  
- **Data Sources:**    
  - `useAnalyticsStore` → `topics`, `sentimentTrend`  
  - Types: `SentimentTrendPoint`, `TopicMetricsResponse`  
- **Key UI:** Sentiment line chart, DataGrid for topics, top topics horizontal bar chart  
- **Loading States:** `TablePlaceholder`, `HorizontalBarsPlaceholder`, `ChartPlaceholder`  
  
### **OperatorInboxView**  
- **Audience:** Operator (primary), also accessible to Creators  
- **Purpose:** Conversation-first UI for real-time chat  
- **Data Sources:** `useChatStore` for active conversations/messages, sentiment, enrichment  
- **Key UI:** Conversation list, message stream, Fan360 enrichment panel  
  
### **GraphExplorerView**  
- **Audience:** Creator role only    
- **Purpose:** Experimental graph-query interface for exploring fan data  
- **Data Flow:** User inputs → simulated AI/Gremlin query → result bubbles  
- **Key UI:** `QueryInput`, `UserQueryBubble`, `QueryResponseBubble`  
- **Loading States:** `QueryResponseBubbleSkeleton`  
  
---  
  
## 🎨 Design Tokens  
  
Implements **3-tier token architecture**:  
  
1. **Tier 1 — Global Tokens** (`tokens.json`)  
2. **Tier 2 — Semantic Tokens** (`generated/theme.ts`)  
3. **Tier 3 — Component Tokens** (MUI `components` overrides)  
  
```bash  
npm run generate:theme   # Build theme from tokens.json  
npm run watch:tokens     # Rebuild theme on token changes  
```  
  
---  
  
## 📐 Type System  
  
- **WS Types:** Generated from backend JSON Schemas (`backend-wss.ts`)  
- **REST Types:** Generated from OpenAPI (`backend.ts`)  
- **Theme Augmentation:** `mui.d.ts` adds `brandPalette`, `effects`, `layout` to `Theme`  
- **Path Aliases:** From `tsconfig.json` — e.g. `@components/...`, `@views/...`  
  
```bash  
npm run sync:wss   # WS types  
npm run sync:rest  # REST types  
npm run sync:all   # both + postprocess  
```  
  
> ⚠️ Never edit generated type files manually.  
  
---  
  
## 🛠 Scripts & Commands  
  
From `package.json`:  
  
| Script | Purpose |  
| --- | --- |  
| `dev` | Generate theme, start Vite dev server |  
| `build` | Typecheck, lint, build theme, production build |  
| `preview` | Preview production build locally |  
| `generate:theme` | Build theme from `tokens.json` |  
| `watch:tokens` | Watch tokens.json & rebuild theme |  
| `sync:wss` | Generate WS types from backend |  
| `sync:rest` | Generate REST types from backend |  
| `sync:all` | Sync WS + REST + postprocess |  
| `typecheck` | Run TypeScript compiler (no emit) |  
| `lint` | ESLint with max-warnings=0 |  
  
---  
  
## 🔄 Development  
  
```bash  
npm install  
npm run dev  
```  
Visit `http://localhost:5173` in dev (proxied API/ws) or via FastAPI in prod.  
  
---  
  
## 🏗 Build for FastAPI  
  
```bash  
npm run build  
# Outputs to ../app/static/dist + manifest.json  
```  
  
---  
  
## 📋 Dev Notes  
  
- Ignore `keepalive` WS messages  
- Maintain snapshot–delta ordering  
- Follow `background.default` vs `background.paper` surface convention  
- RBAC enforced at routing layer  