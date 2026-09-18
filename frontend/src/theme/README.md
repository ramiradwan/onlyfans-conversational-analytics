<!-- CODE-VERIFY: Check intent-contracts.ts, generate-theme.ts, token-consumers.ts, createTheme.ts and package scripts before changing these rules. -->

# Bridge intent tokens

Author tokens in `tokens.json`. Do not edit generated TypeScript, CSS, or the marked token blocks in the popup and setup page.

`tier2.intents` owns product meaning in each color scheme. Its `_intent` references share policies from `tier2._intentContracts`. The generator validates those records before removing metadata. `tier2.colorSchemes` is a checked compatibility adapter; its color fields must reference the corresponding intent paths.

Palette intents require authored `main`, `light`, `dark`, and `contrastText` values. Generation validates the foreground choice; it does not select one. Named role groups hold text, surfaces, communication, sentiment and chart colors.

## Use the roles

MUI adapts action intents to `primary` and `secondary`, and feedback intents to `success`, `warning`, `error` and `info`. Use `measurement` for measured values. Existing `accent` and `calm` names remain deprecated compatibility adapters. Do not use them for new components.

Components use `theme.vars.palette` or semantic `sx` paths. Static surfaces receive `--dipsy-intent-*` variables. Existing `--dipsy-color-*` variables retain their values during migration.

Financial colors have a separate, reserved source family. They are validated but omitted from runtime tokens, MUI and static CSS. Token aliases into that family and explicit financial-token references in consumers fail checks. Existing chart opportunity and categorical colors are unchanged in this appearance-preserving batch; they do not alias financial tokens.

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
