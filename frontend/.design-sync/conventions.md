# Design conventions

## Required root setup

Wrap every composition of bundled components in the bundled `ThemeProvider` with the bundled `theme`; without it, components that read `theme.vars`, `theme.effects`, color schemes, or typography can render incorrectly or throw.

```tsx
const { ThemeProvider, theme } = window.BridgeDesignSystem;

<ThemeProvider theme={theme} defaultMode="light" disableTransitionOnChange>
  <CreatorDashboardView />
</ThemeProvider>
```

Light is the first-run mode; `ThemeToggle` persists the viewer's choice. Components that render navigation links or route outlets (`AppDrawer`, `AppShell`, `SetupPrompt`, `RecentConversations`, and full-app compositions that include them) also need a React Router context. Use `MemoryRouter` for prototypes and a browser router in a real app.

## Copy

Write for a creator who knows nothing about how the product works.

- Use plain words and short sentences. Say "browser extension", "desktop app", "insights", "message history", "stored messages", and "activation code". Never show internal service names, protocol fields, state names, revisions, or error codes.
- Say a thing once. A heading, its one-line summary, and an alert must not repeat each other. Leave out explanations the viewer does not need to act.
- Name buttons after what happens: "Download messages", "Turn off archive", "Continue setup". Avoid "OK", "Submit", and "Learn more".
- Status labels are one or two words ("Up to date", "Updating", "Not connected", "Needs attention"). The sentence that explains a problem sits next to the problem, not in the label.
- Derive every status sentence from structured state. Never render diagnostic text from a service.
- Describe data handling exactly as the product implements it. Do not promise automatic deletion, syncing, or analysis that the flow does not perform.
- Loading copy is "Processing your data…"; empty states say what will appear and when.

## Progressive disclosure

- Lead with the one thing the viewer needs now: a summary, a current status, or a single next step. Give each surface one primary action.
- Before asking for an action (connect, activate, allow history, delete), state its benefit or consequence in one sentence, so the answer is obviously yes or obviously no.
- Setup is a short numbered list with the current step emphasized and finished steps receding (`SetupPrompt`). Show only the current step's details.
- Reveal detail through state, not stacked expanders. Open a multi-step task (activation, turning on the archive) in a focused dialog; put small details and filters in a popover; offer an alternate view such as a data table through a Chart/Table toggle. Do not use accordions or expandable sections.
- A destructive action is a plain settings row with a quiet error-colored button that opens a confirmation dialog.
- Hide sections that have nothing to show (`RecentConversations` renders nothing without conversations) instead of filling the page with placeholders.
- Keep a problem that blocks new data visible as an in-page `Alert`; show calm states with a `StatusChip` only.

## Where controls live

The product has three surfaces. Each control has one owner; other surfaces point to it in plain words ("Open the browser extension and choose Pair device") instead of repeating it.

- **Browser extension popup**: everything Chrome grants or holds: site access, the Chrome permission for message history, Preview mode and its counts, pausing and resuming analytics, pairing with the desktop app, starting Full analytics activation, and deleting extension data. It must work on its own for people who use only the extension.
- **Desktop setup page**: the first-time steps that get Full analytics ready on the computer.
- **Desktop app**: numbers, conversations, and insights; starting, pausing, and turning off message history; stored messages (archive, download, delete); the connection status of the browser extension; and Full analytics status and activation codes.

## Styling idiom

This is a MUI v9 theme/prop system, not a utility-class library. Compose the bundled React components, use MUI layout primitives for glue, and style through `sx` and theme-aware props. Do not invent CSS class names.

- Primitives: take `Alert`, `Box`, `Button`, `Card`, `CardContent`, `Chip`, `Divider`, `Grid`, `IconButton`, `Link`, `Paper`, `Skeleton`, `Stack`, `TextField`, `Tooltip`, and `Typography` from `window.BridgeDesignSystem`. A separate MUI copy does not see the bundled theme and renders with stock MUI styles.

- Surfaces: content and workspace backgrounds use `background.default`; cards, drawers, and panels use `background.paper`. Content surfaces stay opaque. `theme.effects.glassmorphism(theme)` is for navigation and app-bar chrome only. `theme.effects.ambientGlow(theme)` is for setup, empty, and success moments only.
- Cards: the theme already styles MUI cards; use `Panel` or `theme.effects.cardBorder(theme)` for custom panels and `theme.effects.chartFrame(theme)` for chart frames.
- Radius shows hierarchy: surface radius for page-level surfaces, control radius for buttons, inputs, and menus, compact radius for small inline objects, and pill shapes only for status.
- Text: use `text.primary`, `text.secondary`, and `text.disabled`. Keep normal-size text in `text.*` roles unless contrast is verified; accent and info are emphasis and data roles.
- Brand roles: `primary`, `secondary`, `accent`, `calm`, `success`, `warning`, `error`, and `info`. Prefer semantic roles over raw hex values.
- Status: use `StatusChip` with a `tone`; the colored dot carries the tone so the label stays short. Success is deliberately quiet.
- Settings: build each card from `SectionHeader` (title, one-line summary, status chip) and `SettingRow` (label, short description, action at the end), separated by dividers.
- Numbers: use the `kpi` typography variant for the single headline number and `metric` for secondary numbers, with tabular numerals. `DashboardOverview` shows one dominant total, the message volume, and its received/sent split.
- Conversation UI: use `theme.vars.palette.communication.incomingSurface`, `incomingBorder`, `outgoingSurface`, and `outgoingBorder`.
- Analytics: use `theme.vars.palette.chart.categorical1` through `categorical8` in fixed order for identity, and `positive`, `neutral`, `negative`, and `unknown` for sentiment. Keep chart labels in text colors, keep legends for multiple series, and provide a table equivalent.
- Layout: use `theme.spacing(n)`, responsive `sx`, and theme breakpoints. The shell uses `componentTokens.shell` (64 px desktop rail, 264 px mobile drawer, 72 px header, 1320 px dashboard maximum).
- Typography: use MUI variants (`h4`, `h5`, `h6`, `subtitle1`, `subtitle2`, `body1`, `body2`, `caption`). Inter ships with the design system.
- Motion: 120 ms for control feedback, 200 ms for UI transitions, and 320 ms for movement across the layout. Animate transform and opacity only, and keep visible focus treatment.

## Extension popup and setup page

The browser extension popup and the desktop setup page are plain HTML and CSS, not React. They use the same design tokens as CSS custom properties with the `--dipsy-` prefix (for example `--dipsy-color-primary`, `--dipsy-color-paper`, `--dipsy-radius-control`, `--dipsy-space-unit`, `--dipsy-shadow-raised`), with dark values applied through `prefers-color-scheme`. Mock these surfaces with the same cards and pill status badges as the app, at the popup's 390 px width. Secondary popup screens (connection details, extension management) open as separate views with a Back button. Required legal and data-handling text on these surfaces keeps its wording and prominence.

## Sources of truth

Before styling, read `_ds/styles.css` and its imports, especially `_ds/_ds_bundle.css` and `_ds/fonts/fonts.css`. Read each component's `_ds/components/<group>/<Name>/<Name>.d.ts` for props and `<Name>.prompt.md` for usage. The bundled `theme` export is authoritative for semantic palettes, spacing, typography, effects, component overrides, and light and dark color schemes; the bundled `componentTokens` export is authoritative for component and shell dimensions.

## Idiomatic composition

```tsx
const { Box, Button, DashboardOverview, Panel, SectionHeader, SettingRow } = window.BridgeDesignSystem;

<Box sx={{ bgcolor: 'background.default', p: 3 }}>
  <DashboardOverview
    conversations="128"
    messages="2,436"
    received="1,402"
    sent="1,034"
    split={{ received: 1402, sent: 1034 }}
  />
  <Panel sx={{ mt: 3 }}>
    <SectionHeader summary="Saved only on this computer." title="Stored messages" />
    <SettingRow
      action={<Button size="small" variant="outlined">Download messages</Button>}
      description="Save your stored messages to a file."
      title="Download a copy"
    />
  </Panel>
</Box>
```

## Analytics composition

`CreatorDashboardView` and `OperatorInboxView` accept an optional read-only store. Omit it in the live app; for a prototype use the bundled `createPreviewBridgeStore()` helper (`<CreatorDashboardView store={createPreviewBridgeStore()} />`) or an equivalent deterministic `getState`/`subscribe` adapter. Dashboard values come only from the product's analytics and visible conversations: do not invent revenue, spend, subscribers, conversion, period deltas, or generated answers. Keep the loading, live, refreshing, delayed, and unavailable states truthful, and keep the latest complete numbers visible while a refresh runs.
