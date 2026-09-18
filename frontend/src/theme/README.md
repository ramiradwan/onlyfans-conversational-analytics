<!-- CODE-VERIFY: Check intent-contracts.ts, generate-theme.ts, token-consumers.ts, chart-qualification.ts, createTheme.ts and package scripts before changing these rules. -->

# Bridge intent tokens

Author tokens in `tokens.json`. Do not edit generated TypeScript, CSS, or the marked token blocks in the popup and setup page.

`tier2.intents` owns product meaning in each color scheme. Its `_intent` references share policies from `tier2._intentContracts`. The generator validates those records before removing metadata. `tier2.colorSchemes` is a checked compatibility adapter; its color fields must reference the corresponding intent paths.

Palette intents require authored `main`, `light`, `dark`, and `contrastText` values. Generation validates the foreground choice; it does not select one. Named role groups hold text, surfaces, communication, sentiment and chart colors.

## Use the roles

MUI adapts action intents to `primary` and `secondary`, and feedback intents to `success`, `warning`, `error` and `info`. Use `measurement` for measured values. Existing `accent` and `calm` names remain deprecated compatibility adapters. New application consumers of their palette paths fail validation; the adapters remain available for compatibility.

Components use `theme.vars.palette` or semantic `sx` paths. Static surfaces receive `--dipsy-intent-*` variables. Existing `--dipsy-color-*` variable names remain compatible and resolve through the active intents.

Financial colors have a separate, reserved source family. They are validated but omitted from runtime tokens, MUI and static CSS. Token aliases into that family and explicit financial-token references in consumers fail checks. Analytical opportunity uses measurement jade. The fourth categorical slot uses slate instead of financial yellow. No public intent may reuse the reserved financial field color.

## Color validation

Author design colors in OKLCH. Essential roles require sRGB and color-independent meaning. Display-P3 is allowed only for explicitly decorative policies; this batch introduces no P3 colors.

Color.js runs only in build tooling and tests. Gamut checks preserve the existing `5e-4` linear-channel rounding tolerance. Contrast checks composite translucent colors over their stated backgrounds before measuring relative luminance. Generated color strings are not gamut-mapped or rewritten.

Durable color roles require named tokens, not runtime `var()` or mixing expressions in their source values. Declared transient operations remain available through MUI native color. Do not call `augmentColor()` in the Bridge adapter or import validation modules into application code.

## Regenerate and check

From `frontend/`:

```sh
npm run sync:static-tokens
npm test
npm run build
npm run typecheck:design-sync
npm run check:unused
```

The generator checks token-reference provenance, policy shape, gamut, required contrast pairs and reserved consumer references. Component tests and browser captures still verify labels, interaction, focus, reduced motion and the rendered result; metadata is not an accessibility certificate.

`tests/fixtures/theme-appearance-baseline.json` pins the legacy exports and static declarations from `4510a838a125744bb8dadaff977f7239791da663`. Change it only with a separately reviewed visual change. Do not refresh it to make an unintended difference pass.

## Pleasure Pass foundations

Warm neutrals and jade now carry surfaces, measurements and primary actions. Light and dark values are authored independently. The `brand` intent preserves the existing violet mark separately from actions.

Space Grotesk is bundled locally for `kpi`, `metric`, `insight`, numeric captions and counts. Inter remains the default face for prose, headings and labels. The reviewed passkey title is the only display-heading exception. Design previews load both local font packages.

Use `Panel` emphasis to distinguish the one dominant surface from secondary content and quiet rows. Status styling preserves labels, feedback meanings and live announcements. This phase adds no revenue display, first-insights ceremony or readiness transition.

The historical appearance fixture is unchanged. Compatibility checks retain its brand, layout, shape, static prose and motion constraints. Palette, effects and numeric typography are deliberate changes covered by Pleasure Pass tests and browser captures.

## Review contracts

The desktop rail uses opaque paper. Its item width derives from the rail width and the difference between surface and control radii. Hover is weaker than selection; keyboard focus uses the authored hover fill and a visible ring. Icon buttons keep pointer feedback but omit the default animated keyboard ripple. Tooltip colors and both brand tiles use shared intents.

Motion keyframes live in the token source and run through `presentationMotion.ts`. Arrivals use transform and opacity only, last at most the spatial duration including delay, and stop under reduced motion. The healthy app status settles once; shared section statuses do not animate by default.

The header remains 72px and opaque. Dashboard and Stored messages glows remain outside the approved glow scope. Do not infer a freshness status for Stored messages or a tone column for Topics from the reference template; their current data does not provide those fields.

The visual workflow records computed review measurements in `review/acceptance.json`, including rail geometry, state contrast, focus restoration, popup brand parity, numeric roles, aligned topic tracks and reduced-motion behavior. Its checks supplement the ordinary screenshot matrix; neither replaces visual review.

## Static surfaces

The popup and setup page load local Latin Inter and Space Grotesk from the same pinned Fontsource packages as Bridge. `static-fonts.ts` embeds the WOFF2 bytes and redistribution notices in their existing CSS, copies the measured fallbacks from `src/index.css`, and generates early font preloads. Setup preloads Inter only; the popup also preloads its numeric face. No new runtime route, external font request, or JavaScript font gate is required.

Run `npm run sync:static-tokens` after changing the font source or static roles. Do not edit the `static-fonts` or `static-font-preloads` blocks directly. The old font-stack variable remains compatible; the new UI and numeric variables include the bundled fallback faces.

Static feedback fields use named, opaque colors. Healthy settled cards use paper and state readiness once in the task heading. Hover, focus, disabled controls, dominant cards and quiet navigation retain separate meanings. Popup disclosures keep their wording and structure. Setup continues to use its existing controller and validation rules.

The visual workflow includes `static-surfaces/acceptance.json` and fold/full screenshots. Setup fixtures are rendered by the existing controller with synthetic responses; they qualify presentation, not signed-grant authority. The checks verify actual rendered fonts, first-frame canvas color, layout shifts, keyboard focus, disclosure visibility and narrow overflow. They retain the limits in `stability-contracts.mjs`; Bridge's separate production-boot diagnostics remain unchanged.

Operational copy names the current task once. Setup feedback appears beside the active control only after a check or failure. Completed steps stay compact. Recovery guidance names an existing route. Use periods, not semicolons. Protected disclosures retain their substance, structure and prominence. The only disclosure edit in this pass replaces a semicolon with a period.
