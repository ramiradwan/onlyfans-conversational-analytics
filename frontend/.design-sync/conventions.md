# Bridge design conventions

## Required root setup

Wrap every app tree in the bundled `ThemeProvider` with the bundled `theme`; without it, components that read `theme.vars`, `theme.effects`, color schemes, or typography can render incorrectly or throw.

```tsx
const { ThemeProvider, theme } = window.BridgeDesignSystem;

<ThemeProvider theme={theme} defaultMode="light" disableTransitionOnChange>
  <App />
</ThemeProvider>
```

For routed shells (`AppDrawer`, `AppShell`, or full views), also provide a React Router context. Use `MemoryRouter` for prototypes and a browser router in a real app.

## Styling idiom

This is a MUI v7 theme/prop system—not a utility-class library. Compose the bundled React components, use MUI layout primitives for glue, and style through `sx` and theme-aware props. Do not invent CSS class names.

- Surfaces: content/workspace backgrounds use `background.default`; chrome, cards, drawers, and panels use `background.paper`.
- Text: use `text.primary`, `text.secondary`, and `text.disabled` through `color` or `sx`.
- Brand roles: use `primary`, `secondary`, `accent`, `calm`, `success`, `warning`, `error`, and `info`; prefer semantic roles over raw hex values.
- Conversation UI: use `theme.vars.palette.communication.incomingSurface`, `incomingBorder`, `outgoingSurface`, and `outgoingBorder`.
- Analytics: use `theme.vars.palette.chart.sentiment`, `volume`, and `neutral`.
- Layout: use `theme.spacing(n)`, responsive `sx`, and theme breakpoints. Cards and panels should use `theme.effects.cardBorder(theme)`; chart frames use `theme.effects.chartFrame(theme)`; overlays use `theme.effects.glassmorphism(theme)`.
- Typography: use MUI variants (`h4`, `h6`, `subtitle1`, `body1`, `body2`, `caption`). Inter is shipped with the design system.
- Interaction: preserve visible focus treatment, calm border-based surfaces, and motion no longer than the theme's 200 ms token.

## Sources of truth

Before styling, read `_ds/styles.css` and its imports, especially `_ds/_ds_bundle.css` and `_ds/fonts/fonts.css`. Read each component's `_ds/components/<group>/<Name>/<Name>.d.ts` for props and `<Name>.prompt.md` for usage. The bundled `theme` export is authoritative for semantic palettes, spacing, typography, effects, component overrides, and light/dark color schemes.

## Idiomatic composition

```tsx
const { KpiCard, Panel } = window.BridgeDesignSystem;

<Box sx={{ bgcolor: 'background.default', p: 3 }}>
  <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
    <KpiCard title="Avg. response time" value="4.8 min" grow />
    <KpiCard title="Overall sentiment" value="84%" grow />
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
