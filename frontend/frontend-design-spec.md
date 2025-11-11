# 🎨 Frontend UX/UI Design Specification    
**TO:** Product Designers, Frontend Engineers, AI Code Generators    
**FROM:** Principal Product Designer    
**DATE:** 2025‑11‑11    
**VERSION:** 3.5    
**SUBJECT:** Human‑Centered UX & Visual System Specification for the Frontend    
  
---  
  
## Introduction & Purpose  
  
This document defines the **authoritative design and implementation plan** for the OnlyFans Conversational Analytics frontend (“Bridge”).  
  
It is both:    
- A **UX/UI style guide**: principles, visual language, accessibility, trust, and calm interface rules    
- An **engineering reference**: component architecture, MUI patterns, WS/REST event mapping  
  
**Alignment:**    
- **Creator‑first**, role‑specific, calm in presentation    
- **Aligned** with [`AI-instructions.md`](/AI-instructions.md) and [`communication-spec.md`](/communication-spec.md)    
- **Mapped** to backend WS/REST contracts for type‑safe integration    
- **Grounded** in practical React + Vite + MUI patterns    
- **Conversation‑centric**: every dashboard and sidebar is driven by conversation data and analytics  
  
**Design Goal:**    
> Build an information‑dense yet calm interface that helps creators and their teams act efficiently, think clearly, and feel confident managing audience and team workflows — with conversation analytics at the core.  
  
---  
  
## Part A — UX/UI Principles & Style Guide  
  
### 1. UX Philosophy  
| Principle                   | Description                                                                   | Example |  
| --------------------------- | ----------------------------------------------------------------------------- | ------- |  
| Clarity Beats Cleverness    | Plain language, clear icons, obvious affordances                              | “View Analytics” not “Crunch the Numbers” |  
| Design for Stress           | Optimize for speed/accuracy in multitasking                                   | Prominent CTAs, minimal decision fatigue |  
| Calm Interfaces             | Reduce cognitive load via whitespace, progressive disclosure                 | Show top 3 insights first, expand for details |  
| Role‑Specific Interfaces    | Tailor flows/dashboards to user roles                                         | Creator sees insights; operator sees team performance |  
| Transparency Builds Trust   | Explain actions, states, permissions                                          | “Message saved locally” vs “Success” |  
| Accessibility Is Default    | WCAG 2.2 AA minimum compliance                                                | Contrast ≥ 4.5:1; full keyboard access |  
| Feedback as Reassurance     | Calm, consistent feedback for every action                                    | “Data saved — updating chart” toast |  
| Conversation‑First Design   | Prioritize conversation data in all views                                    | Inbox sorted by priority score, Fan360 shows live sentiment |  
  
---  
  
### 2. Role‑Based UX Architecture & Personas  
| Role        | Primary Goals                             | UX Priorities                              |  
| ----------- | ----------------------------------------- | ------------------------------------------ |  
| Creator‑CEO | Understand engagement, triage fans        | Calm dashboards, actionable conversation insights |  
| Manager     | Oversee accounts, team performance        | Predictive summaries, error visibility     |  
| Operator    | Communicate quickly, manage fans          | Minimal navigation friction, responsive input |  
  
**Guideline:** Each role gets a **dedicated layout** and **reduced scope** to prevent overload.  
  
**Persona Highlights:**    
- **Creator‑CEO (“Alex”)** — needs high‑level sentiment/topic trends, priority inbox, revenue KPIs.    
- **Operator (“Marco/Sarah”)** — needs chatter leaderboard, fan segmentation, per‑fan sentiment, sales funnel conversion from conversations.  
  
---  
  
### 3. Visual Language  
**Colors:** Soft, muted palette to reduce visual stress; contrast ≥ 4.5:1.    
| Token                     | Use                     | Example |  
| ------------------------- | ----------------------- | ------- |  
| `--color-primary`         | Action / CTA            | #2563EB |  
| `--color-success`         | Positive feedback       | #16A34A |  
| `--color-warning`         | Attention needed        | #FACC15 |  
| `--color-error`           | Critical error          | #DC2626 |  
| `--color-surface`         | Background              | #FFFFFF / #111827 |  
| `--color-muted`           | Secondary text          | #6B7280 |  
| `--color-bubble-outbound` | Creator message bubble  | #d8f5ff |  
| `--color-bubble-inbound`  | Fan message bubble      | #f3f3f3 |  
  
**Typography:** `"Inter", Helvetica Neue, sans-serif`    
- H1: 32px / 700 — Page title    
- H2: 24px / 600 — Section title    
- Body: 16px / 400    
- Caption: 14px / 400    
  
**Iconography:**    
- Lucide/Heroicons set    
- Clear metaphors (chat bubble, clipboard)    
- Consistent stroke width and corner radius  
  
---  
  
### 4. Interaction & Behavior  
| State    | Feedback                    |  
| -------- | ---------------------------- |  
| Hover    | Subtle elevation/tint        |  
| Focus    | 2px outline (accessible color) |  
| Active   | Slight scale compression     |  
| Disabled | Opacity reduction, pointer‑off |  
  
Animation: fade + slide ≤ 200 ms, respect `prefers-reduced-motion`.  
  
---  
  
### 5. Accessibility & Inclusivity  
- WCAG 2.1+ compliance    
- Full keyboard navigation    
- `aria-label` and `role` on interactive elements    
- Screen‑reader support for dynamic content    
- Color‑blind safe palette    
- Internationalization‑ready layouts    
- Inclusive language, avoid jargon/slang  
  
---  
  
### 6. Cognitive Load Reduction  
- ≤ 7 actionable elements per view    
- Progressive disclosure for complexity    
- Summaries first, details on demand    
- Group related data in collapsible sections    
- Contextual tooltips > modal help  
  
---  
  
### 7. Privacy, Security, & Trust  
- Role‑based access control in UI    
- Clear data usage/privacy settings    
- Consent before importing/analyzing conversations    
- Connection status indicators for real‑time features    
- Auto‑logout after inactivity for sensitive actions  
  
---  
  
### 8. Dashboard Layout Principles  
- Essential info up‑front: KPIs on main screen    
- Simple charts with context: consistent colors, clear labels    
- Actionable insights first: priority fans/tasks prominent    
- Balanced text/visuals: short captions with data viz    
- Support exploration: drill‑downs, filters, breadcrumb navigation  
  
---  
  
## Part B — Implementation Patterns & Data Integration  
  
### 9. Navigation & Layout  
- Persistent `<Drawer>` (collapsible < 1024px)    
- Top bar: search, filters (sentiment, topics, unread), system status    
- Breadcrumbs in nested routes    
- Mobile back button in conversation view    
- ≤ 2 clicks from KPI → conversation detail  
  
---  
  
### 10. RBAC Implementation  
Global state: `user.role` in Zustand    
Hook:    
```ts  
import { useStore } from '@store/useChatStore';  
export const usePermissions = () => {  
  const role = useStore(s => s.user.role);  
  return {  
    isCreator: role === 'creator-ceo',  
    isManager: role === 'agency-manager',  
    isOperator: role === 'chatter',  
    canViewRevenue: role === 'creator-ceo' || role === 'agency-manager',  
    canDeleteConversation: role === 'creator-ceo',  
    canExportChat: role === 'creator-ceo',  
    canMassMessage: role !== 'chatter'  
  };  
};  
```  
Guard:    
```tsx  
import { usePermissions } from '@hooks/usePermissions';  
export const PermissionGuard = ({ children, requires }) => {  
  const permissions = usePermissions();  
  return permissions[requires] ? <>{children}</> : null;  
};  
```  
  
---  
  
### 11. Component Architecture (MUI)  
**App Root:**    
```tsx  
<Box sx={{ display: 'flex', height: '100vh' }}>  
  <GlobalLoadingSpinner />  
  <Box component="main" sx={{ flexGrow: 1, p: 3 }}>  
    {isCreator && <CalmTriageView />}  
    {isManager && <PerformanceView />}  
    {isOperator && <InboxView />}  
  </Box>  
</Box>  
```  
  
**InboxView:**    
- ChatList sorted by backend priority score (LTV + sentiment + unread)    
- Filters: unread, top fans, negative sentiment, online now  
  
**MessageView (3‑Pane):**    
```tsx  
<Grid container>  
  <Grid item md={3} xs={12}><ChatList /></Grid>  
  <Grid item md={6} xs={12}>  
    <Stack spacing={1}>  
      {messages.map(msg => (  
        <MessageBubble key={msg.id} msg={msg} />  
      ))}  
    </Stack>  
  </Grid>  
  <Grid item md={3} xs={12}><Fan360Sidebar /></Grid>  
</Grid>  
```  
  
**MessageBubble:**    
- Text, timestamp, sender role    
- Sentiment badge (color + icon + score) from enrichment  
  
**Fan360Sidebar:**    
- Profile: username, join date, badges    
- Vitals: LTV, Avg Tip, Rebill status    
- Conversational Analytics: live sentiment gauge, top topics, AI conversation summary    
- Notes: `<TextField multiline>`    
- Purchase History: lazy‑load via REST  
  
---  
  
### 12. WS/REST → UI Mapping *(per communication‑spec.md)*  
**WS Types:**    
| WS Type             | Component(s)             | Action |  
| ------------------- | ------------------------ | ------ |  
| `connection_ack`    | App bar                   | Show connected state/version |  
| `system_status`     | GlobalLoadingSpinner      | Show/hide backdrop |  
| `system_error`      | Snackbar                  | Error toast |  
| `full_sync_response`| Dashboards, Inbox, Fan360 | Replace conversation & analytics state |  
| `append_message`    | MessageView, Fan360       | Append message, update sentiment/topics |  
| `analytics_update`  | KPI widgets, Fan360       | Update metric |  
| `command_to_execute`| ExtensionService          | Forward to extension |  
  
**REST Endpoints:**    
- `/analytics/ceo_dashboard`    
- `/analytics/operator_dashboard`    
- `/analytics/sales_funnel`    
- `/fan/{id}/purchase_history`    
- `/fan/{id}/summarize`  
  
**Data Models:**    
- `ConversationNode`: id, messages[], sentimentProfile, topics[], vitals    
- `Message`: id, text, timestamp, sender, sentimentScore, topics[]    
- `AnalyticsUpdate`: metricName, value, targetId  
  
---  
  
### 13. Loading & Error UI  
GlobalLoadingSpinner:    
```tsx  
<Backdrop open={status === 'PROCESSING_SNAPSHOT'} sx={{ zIndex: theme => theme.zIndex.drawer + 1 }}>  
  <CircularProgress color="inherit" />  
</Backdrop>  
```  
Error Snackbar:    
```tsx  
<Snackbar open={errorOpen} autoHideDuration={4000}>  
  <Alert severity="error">An error occurred</Alert>  
</Snackbar>  
```  
Use `<Skeleton>` in KPI cards and Fan360Sidebar during data load.  
  
---  
  
### 14. MUI Theme (`src/theme.ts`)  
```ts  
export const theme = createTheme({  
  palette: {  
    primary: { main: '#2563EB' },  
    success: { main: '#16A34A' },  
    warning: { main: '#FACC15' },  
    error: { main: '#DC2626' },  
    background: { default: '#F9FAFB', paper: '#FFFFFF' },  
  },  
  typography: {  
    fontFamily: '"Inter", "Roboto", "Helvetica", "Arial", sans-serif',  
    h1: { fontSize: '2.5rem', fontWeight: 600 },  
    h2: { fontSize: '2rem', fontWeight: 600 },  
  },  
});  
```  
Dark mode:    
```ts  
palette: { mode: 'dark', background: { default: '#1F2937', paper: '#374151' } }  
```  
  
---  
  
### 15. MUI + React Best Practices  
- Wrap app in `<ThemeProvider theme={theme}>`    
- Use `sx` prop for quick theme‑aware adjustments    
- Prefer `<Grid>` + breakpoints for responsive layouts    
- Lazy‑load heavy components with `React.lazy` + `<Suspense>`    
- Test component performance with React Profiler + Lighthouse    
- Abstract common UI patterns into reusable components  
  
---  
  
### 16. QA Checklist  
1. Role gating verified against backend RBAC    
2. Accessibility audit: Lighthouse ≥ 95    
3. Performance: FCP < 1 s, TTI < 2.5 s    
4. Theme token consistency    
5. Dark mode semantic check    
6. Calmness audit: ≤ 7 elements per view    
7. WS/REST events correctly update conversation components    
8. Snapshot–delta ordering verified (no race conditions)    
9. Sentiment/topics update correctly on `append_message`    
10. Generated type files match backend schemas    
11. `keepalive` messages ignored in UI    
12. Priority sorting matches backend score  
  
---  
  
**✅ End of v3.5 Specification**  