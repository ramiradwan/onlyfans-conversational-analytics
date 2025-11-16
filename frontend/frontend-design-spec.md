# 🎨 **FRONTEND UX/UI DESIGN SPECIFICATION**

**Project:** OnlyFans Conversational Analytics (“Bridge”)  
**Author:** UX/UI Specialist · Principal Product Designer  
**Date:** 2025-11-16  
**Version:** 7.1

-----

## **1. Overview**

### **1.1 Purpose**

Defines the **single source of truth** for Bridge’s frontend UX, UI, interaction model, component architecture, and real-time data behavior — now aligned to a **formal 3-tier token architecture**, **MUI v7 CSS variables system**, and **integrated WCAG 2.2 Level AA compliance**.

Serves as:

  * **Design System Reference** — principles, components, accessibility, motion
  * **Frontend Engineering Contract** — theme tokens, layouts, RBAC, WS/REST mapping, token governance rules

### **1.2 Scope**

**In-Scope**

  * Conversational analytics dashboard
  * Fan360 intelligence panel
  * Inbox + conversation workflows
  * Role-based dashboards for **Creator** and **Operator**

**Out of Scope**

  * Backend data processing/model training
  * Billing and CRM integrations

**Constraints**

  * Browsers: Chrome / Safari ≥ 2023
  * Viewports: ≥ 1280px desktop + adaptive tablet
  * Compliance: WCAG 2.2 AA (required)

### **1.3 Success Metrics**

| Metric | Target |
| :--- | :--- |
| Lighthouse UX | ≥ 90 |
| Usability: task success | ≥ 95% |
| FCP | \< 1s |
| TTI | \< 2.5s |
| Max actionable items per view | ≤ 7 |

-----

## **2. Product Goals & UX Principles**

**Primary Goal:**
Deliver a **calm, information-dense**, high-trust interface that enables creators and teams to interpret conversations, assess audiences, and act decisively with minimal cognitive load.

| Principle | Description | Implementation Examples |
| :--- | :--- | :--- |
| **Clarity \> Cleverness** | Plain language, predictable actions | Buttons labeled “View Analytics” not “Crunch Numbers” |
| **Stress-Optimized** | Assist multitasking and accuracy | High-visibility CTAs, reduced decision surfaces |
| **Calm Interfaces** | Whitespace + progressive disclosure | Show top insights first; drill-down on demand |
| **Role-Targeted** | Surfaces change dynamically by persona | Operator sees Inbox-first; Creator sees Insights-first |
| **Transparency = Trust** | Show system state transitions | “Synced”, “Saving…”, “Reconnected” |
| **Accessible by Default** | WCAG AA mandatory | Contrast ≥ 4.5:1, full keyboard flow |
| **Feedback as Reassurance** | Microfeedback on all actions | Save toasts, message-ready indicators |
| **Conversation-First** | Conversations anchor navigation | Inbox prioritized by sentiment + LTV |

-----

## **3. Users & Personas**

### **3.1 Roles**

| Role | Primary Goals | UX Priorities |
| :--- | :--- | :--- |
| **Creator-CEO (“Alex”)** | Understand engagement, triage fans | Calm dashboards, actionable insights |
| **Operator (“Marco/Sarah”)** | Communicate efficiently | Minimal friction, responsive chat |

### **3.2 Persona Extensions**

| Persona | Motivations | Pain Points | Behaviors |
| :--- | :--- | :--- | :--- |
| **Creator-CEO** | Wants strategic clarity | Overwhelmed by complex dashboards | Logs in weekly, skims insights |
| **Operator** | Wants to hit goals fast | Dislikes cluttered, laggy UIs | Chats continuously, multitasks heavily |

-----

## **4. Requirements**

### **4.1 Functional**

  * Filter conversations (unread, sentiment, fan value)
  * Real-time analytics via WebSocket
  * RBAC-driven UI layouts and routing
  * Progressive disclosure for analytics layering

### **4.2 Non-Functional**

  * WS round-trip \< 200ms
  * Keyboard + screen-reader fully supported
  * Responsive 768–1920px
  * Motion ≤ 200ms; honors `prefers-reduced-motion`

-----

## **5. Information Architecture**

### **5.1 Primary Navigation**

  * Dashboard (Creator-only)
  * Inbox (Conversation-first)
  * Analytics (Creator-only)
  * Settings

**Implementation:** Navigation is managed by `react-router-dom`. The `AppDrawer`'s `<ListItemButton>` components are `NavLink` components, mapping their `active` state to the `selected` prop. The URL is the single source of truth for the user's location.

### **5.2 Core Flow**

**KPI → Fan Analytics → Conversation Thread → Sentiment → Action**

### **5.3 Layout Patterns**

  * **Persistent `AppShell`:** The core layout consists of a persistent `AppShell` component containing the `AppAppBar` and `AppDrawer`. This shell renders a `react-router-dom` `<Outlet />`, which displays the active, role-specific view.
  * **"Calm" Surface Convention:** To support the "Calm Interfaces" principle (2.0), the layout strictly differentiates "chrome" (navigation/containers) from "content."
      * **Chrome (`background.paper`):** `AppAppBar`, `AppDrawer`, `Card`, and `Paper` surfaces.
      * **Content (`background.default`):** The main `<Box component="main">` that hosts the `<Outlet />` and the background of the `MessageStreamPane`.
  * Bento Grid for KPIs and insights.
  * Left drawer (persistent at ≥1024px, collapsible below).
  * ≤ 2 clicks from KPI → conversation detail.

-----

## **6. Visual Language (2025–2026)**

### **6.1 Token Architecture**

| Tier | Purpose | Spec Location | MUI Mapping |
| :--- | :--- | :--- | :--- |
| **Tier 1: Global / Reference Tokens** | Raw, immutable design decisions (brand hex, font sizes, spacing primitives, effects). Never used directly in components. | `theme.brandPalette`, `theme.brandTypography`, `theme.effects` | Custom top-level keys |
| **Tier 2: Semantic / Alias Tokens** | Contextual roles that adapt per mode (light/dark); reference Tier 1. Drive majority of styling. | `theme.palette`, `theme.typography`, `theme.spacing` | MUI native theme keys |
| **Tier 3: Component Tokens** | Scoped to specific components for unique cases. Governed “escape hatch” to avoid token sprawl. | `theme.components` overrides | MUI component override API |

**Governance Mandates:**

  * 80% of styling from Tier 2; ≤20% from Tier 3.
  * Tier 2 expresses **context/intent**, not component detail.
  * Tier 3 only when semantic tokens are insufficient — no hard-coded `sx` values.
  * Tier 1 never used directly in components — only inside `createTheme`.
  * All token aliases resolved **at build time** — no runtime alias lookups.

### **6.2 Color System**

  * **Tier 1:** Brand colors stored in `theme.brandPalette` (immutable).
  * **Tier 2:** Semantic colors, including custom ones (`accent`, `calm`), added via **`augmentColor()`**:
      * Auto-generate `light`, `dark`, and `contrastText`.
      * Enforce WCAG AA via `contrastThreshold: 4.5`.
      * Available to component `color` props (e.g., `<Button color="accent">`).
  * **Tier 3:** Component-specific variants in `theme.components`.

### **6.3 Typography**

  * Tier 1: Font size primitives in `theme.brandTypography`.
  * Tier 2: Semantic styles in `theme.typography`.

### **6.4 Effects**

  * Tier 1 reusable effects (e.g., `theme.effects.glassmorphism`) with type-safe definitions, reused in modals, loaders, panels.

-----

## **7. Interaction & Feedback**

| State | Behavior |
| :--- | :--- |
| Hover | Subtle elevate/tint |
| Focus | 2px visible outline (AA compliant) |
| Active | `scale(0.97)` |
| Disabled | Opacity reduction |
| Motion | ≤ 200ms / `ease-in-out` |

-----

## **8. Accessibility**

  * WCAG 2.2 AA mandatory:
      * Normal text ≥ 4.5:1
      * Large text ≥ 3:1
      * Non-text UI ≥ 3:1
  * `contrastThreshold: 4.5` in `createTheme` ensures systemic compliance via `augmentColor()`.
  * **Token Contrast Compliance Matrix** must be executed pre-release; failures remediated.

-----

## **9. Cognitive Load Reduction**

  * ≤ 7 actionable elements per view
  * Summaries → details → expert layers
  * Collapsible groups
  * Contextual tooltips instead of help modals

-----

## **10. Privacy & Trust**

  * RBAC content gating
  * Explicit user consent for data import/analysis
  * Real-time connection indicator
  * Auto-logout after inactivity

-----

## **11. Engineering Reference**

### **11.1 Component Architecture (React + MUI)**

```tsx
// App.tsx: Provides Theme, Router, and global state
<ThemeProvider theme={theme}>
  <CssBaseline />
  <GlobalStateProvider> {/* e.g., Zustand */}
    <BrowserRouter>
      <AppRouter />
    </BrowserRouter>
  </GlobalStateProvider>
</ThemeProvider>

// AppRouter.tsx: Selects the correct layout shell
<Routes>
  <Route path="/" element={<AppShell />}>
    {/* Role-based routes from useAppRoutes() are injected here */}
    <Route index element={<RoleSpecificLandingView />} />
    <Route path="inbox" element={<OperatorInboxView />} />
    <Route path="analytics" element={<AnalyticsView />} />
    {/* ...etc */}
  </Route>
  {/* <Route path="/login" element={<LoginView />} /> */}
</Routes>

// AppShell.tsx: The persistent layout
<Box sx={{ display: 'flex', height: '100vh' }}>
  <AppAppBar />
  <AppDrawer />
  <Box 
    component="main" 
    sx={{ 
      flexGrow: 1, 
      p: 3, 
      bgcolor: 'background.default', // "Calm" content area (5.3)
      height: 'calc(100vh - 64px)', // 64px = AppBar height
      overflow: 'auto'
    }}
  >
    <Outlet /> {/* Role-based views are rendered here */}
  </Box>
</Box>
```

### **11.2 PermissionGuard (RBAC)**

```ts
export const usePermissions = () => {
  const role = useStore(s => s.user.role); // 'creator-ceo' or 'chatter'

  return {
    isCreator: role === 'creator-ceo',
    isOperator: role === 'chatter',
  };
};
```

### **11.2a Role-Based Routing**

RBAC is implemented at the **routing layer**, not with conditional rendering.

1.  A `usePermissions()` hook (11.2) provides the user's role.
2.  A `useAppRoutes()` hook consumes `usePermissions()` to generate a role-specific array of `<Route>` definitions.
3.  This enforces the "Operator sees Inbox-first" rule (3.1) by setting the `/` (index) route to `<Navigate to="/inbox" />` for the "Operator" role, while mapping it to `<CreatorDashboardView />` for the "Creator" role.

### **11.3 Theme Configuration — MUI v7 Golden Path**

```ts
export const theme = createTheme({
  cssVariables: { enabled: true, colorSchemeSelector: 'data-mui-color-scheme' },

  // Tier 1: Global Tokens
  brandPalette: {
    groundedTech: { primary: '#2563EB', warmNeutral: '#A47864' },
    optimisticAccent: { primary: '#FACC15', muted: '#707070' },
    calmClear: { primary: '#0062E0', etherealBlue: '#E0F0FF' }
  },

  // Tier 2: Semantic Tokens
  colorSchemes: {
    light: { 
      palette: { 
        primary: { main: '#2563EB' }, 
        background: { default: '#F9FAFB', paper: '#FFFFFF' } 
      } 
    },
    dark: { 
      palette: { 
        primary: { main: '#3B82F6' }, 
        background: { default: '#121212', paper: '#1E1E1E' } 
      } 
    }
  },

  // Tier 3: Component Tokens
  components: {
    MuiButton: {
      styleOverrides: {
        root: ({ theme }) => ({
          backgroundColor: theme.vars.palette.primary.main,
          [theme.getColorSchemeSelector('dark')]: {
            backgroundColor: theme.vars.palette.primary.dark
          }
        })
      }
    }
  }
});
```

-----

## **12. WebSocket / REST → UI Mapping**

| WS Type | State Store | UI Behavior |
| :--- | :--- | :--- |
| `connection_ack` | `userStore` | Sets connected state in `AppBar`. |
| `system_status` | `analyticsStore` | Toggles `GlobalLoader` visibility (`PROCESSING_SNAPSHOT`).|
| `system_error` | `notificationStore`| Fires global `Snackbar` error. |
| `full_sync_response`| `analyticsStore`, `chatStore`| Populates all dashboards and chat lists on initial load. |
| `append_message` | `chatStore` | Appends message to `MessageStreamPane` & updates `ChatListPane`.|
| `analytics_update` | `analyticsStore` | Updates KPI widgets & `DataGrid` in `CreatorDashboardView`.|
| `enrichment_result` | `enrichmentStore` | Populates the `Fan360InsightsPane` for the active conversation.|

-----

## **13. Loading, Error & Motion Patterns**

  * GlobalLoader = Glassmorphism + Lottie
  * Snackbar = centralized error handling
  * Skeletons for KPI & Fan360 load states
  * Motion ≤ 200 ms, `ease-in-out`

-----

## **14. Validation & Testing**

### **14.1 UX Validation**

  * 5 users per role, 90% success threshold
  * Lighthouse accessibility ≥ 95
  * React Profiler + Lighthouse performance targets met

### **14.2 QA Checklist — v7 Tokens**

  * ✅ Tier 1 defined in top-level keys; never used directly in components.
  * ✅ Tier 2 references Tier 1; custom colors via `augmentColor()`; light/dark parity validated.
  * ✅ Tier 3 overrides scoped and governed; interaction/focus states reference Tier 2 tokens.
  * ✅ No `theme.palette.mode` checks; all styles use `theme.vars` and selectors.
  * ✅ WCAG AA compliance matrix executed; failures remediated before release.
  * ✅ All aliases resolved at build time.

-----

## **15. Risks & Mitigation**

| Risk | Impact | Mitigation |
| :--- | :--- | :--- |
| Token Swamp | Design drift | Enforce 80/20 Tier 2/Tier 3 rule |
| Accessibility failure | Compliance risk | Automated contrast checks via matrix |
| Performance regression | Build/runtime cost | No runtime alias resolution; pre-processing in CI |
| Dark mode flicker | UX degradation | CSS variable engine + selectors |

-----

## **16. Glossary**

  * **Tier 1: Global Tokens** — Raw, immutable design decisions stored in top-level theme keys.
  * **Tier 2: Semantic Tokens** — Contextual values in MUI’s `palette`, `typography`, etc.
  * **Tier 3: Component Tokens** — Scoped overrides in `theme.components`.
  * **`AppShell`** — The persistent layout component (`AppBar`, `AppDrawer`) that hosts the `Outlet`.
  * **`Outlet`** — The `react-router-dom` component that renders the active, role-based view.
  * **"Calm" Surface** — The `background.default` (content) vs. `background.paper` (chrome) convention.
  * **Glassmorphism Token** — Reusable Tier 1 effect object for frosted overlays.    
  * **CSS Variable Engine** — MUI v7 system for theme-aware, flicker-free mode toggling.

-----

✅ **End of v7.1 Specification**