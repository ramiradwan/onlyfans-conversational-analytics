# Bridge design conventions

## Required root setup

Wrap every composition of bundled components in the bundled `ThemeProvider` with the bundled `theme`; without it, components that read `theme.vars`, `theme.effects`, color schemes, or typography can render incorrectly or throw.

```tsx
const { ThemeProvider, theme } = window.BridgeDesignSystem;

<ThemeProvider theme={theme} defaultMode="light" disableTransitionOnChange>
  <CreatorDashboardView />
</ThemeProvider>
```

Light is the first-run mode; `ThemeToggle` persists the viewer's choice. For components that render navigation links or route outlets (`AppDrawer`, `AppShell`, and full-app compositions that include them), also provide a React Router context. Use `MemoryRouter` for prototypes and a browser router in a real app.

## Styling idiom

This is a MUI v9 theme/prop system—not a utility-class library. Compose the bundled React components, use MUI layout primitives for glue, and style through `sx` and theme-aware props. Do not invent CSS class names.

- Surfaces: content/workspace backgrounds use `background.default`; chrome, cards, drawers, and panels use `background.paper`.
- Text: use `text.primary`, `text.secondary`, and `text.disabled` through `color` or `sx`.
- Brand roles: use `primary`, `secondary`, `accent`, `calm`, `success`, `warning`, `error`, and `info`; prefer semantic roles over raw hex values. Keep normal-size text in `text.*` roles unless its foreground/background contrast is verified—accent and info are primarily emphasis/data roles.
- Conversation UI: use `theme.vars.palette.communication.incomingSurface`, `incomingBorder`, `outgoingSurface`, and `outgoingBorder`.
- Analytics: use `theme.vars.palette.chart.categorical1` through `categorical8` in their fixed order for identity, and `positive`, `neutral`, `negative`, and `unknown` for sentiment status. Keep chart labels in text colors, retain legends for multiple series, and provide a table equivalent.
- Layout: use `theme.spacing(n)`, responsive `sx`, and theme breakpoints. The modern shell uses `componentTokens.shell` (76 px desktop rail, 264 px mobile drawer, 72 px header, 1320 px dashboard maximum). The bundled theme already gives MUI cards their surface treatment; use `theme.effects.cardBorder(theme)` for custom panels, `theme.effects.chartFrame(theme)` for dedicated chart frames, and `theme.effects.glassmorphism(theme)` for translucent app-bar or overlay chrome.
- Typography: use MUI variants (`h4`, `h6`, `subtitle1`, `body1`, `body2`, `caption`). Inter is shipped with the design system.
- Interaction: preserve visible focus treatment, soft-elevation surfaces, and motion no longer than the theme's 200 ms token.

## Sources of truth

Before styling, read `_ds/styles.css` and its imports, especially `_ds/_ds_bundle.css` and `_ds/fonts/fonts.css`. Read each component's `_ds/components/<group>/<Name>/<Name>.d.ts` for props and `<Name>.prompt.md` for usage. The bundled `theme` export is authoritative for semantic palettes, spacing, typography, effects, component overrides, and light/dark color schemes; the bundled `componentTokens` export is authoritative for component and shell dimensions.

## Idiomatic composition

```tsx
const { KpiCard, Panel } = window.BridgeDesignSystem;

<Box sx={{ bgcolor: 'background.default', p: 3 }}>
  <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
    <KpiCard title="Total conversations" value={4} grow />
    <KpiCard title="Total messages" value={8} grow />
  </Stack>
  <Panel sx={{ mt: 3 }}>
    <Typography variant="h6">Audience insight</Typography>
    <Typography variant="body2" color="text.secondary">
      Positive sentiment rose after the latest content release.
    </Typography>
  </Panel>
</Box>
```

Keep views calm and information-dense: summaries first, details progressively disclosed, no more than seven primary actions, and connection/degraded states always visible in plain language.

## Canonical analytics composition

`CreatorDashboardView` and `OperatorInboxView` accept an optional read-only Bridge store. Omit it in the live app; for a prototype use the bundled `createPreviewBridgeStore()` helper (`<CreatorDashboardView store={createPreviewBridgeStore()} />`) or provide an equivalent deterministic `getState`/`subscribe` adapter. Dashboard values must come from canonical analytics and visible conversations: do not invent revenue, spend, subscribers, conversion, period deltas, or AI answers. Preserve the truthful loading, live, resyncing, cached/degraded, and unavailable states, and keep the latest complete snapshot visible while refreshes occur.
