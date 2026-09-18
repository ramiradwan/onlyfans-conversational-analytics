import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { theme } from '../src/theme/createTheme';
import { semanticIntents } from '../src/theme/generated/tokens';
import { generateStaticTokensCss, generateThemeSource } from '../src/theme/generate-theme';
import { buildTokenGraph, validateBridgeIntents, type JsonObject } from '../src/theme/intent-contracts';
import { validateConsumerSource, validateRepositoryConsumers } from '../src/theme/token-consumers';

const source = fs.readFileSync(path.resolve('src/theme/tokens.json'), 'utf8');
const fresh = (): JsonObject => JSON.parse(source) as JsonObject;
function at(root: JsonObject, address: string): JsonObject {
  return address.split('.').reduce((node, key) => node[key] as JsonObject, root);
}

describe('production intent adapters', () => {
  it('validates every required contract before dropping shared metadata', () => {
    expect(() => validateBridgeIntents(buildTokenGraph(fresh()))).not.toThrow();
    const raw = fresh();
    delete at(raw, 'tier2.intents.light.measurement')._intent;
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/ungoverned|missing/);
  });
  it.each(['light', 'dark'] as const)('routes %s MUI colors and CSS through the same intent values', (mode) => {
    const palette = theme.colorSchemes[mode]?.palette;
    expect(palette?.primary).toMatchObject(semanticIntents[mode].action.primary);
    expect(palette?.measurement).toMatchObject(semanticIntents[mode].measurement);
    expect(palette?.sentiment).toEqual(semanticIntents[mode].sentiment);
    expect(palette?.accent).toMatchObject(semanticIntents[mode].legacy.accent);
    expect(palette).not.toHaveProperty('financial');
    const blocks = generateStaticTokensCss(source).split('@media (prefers-color-scheme: dark)');
    expect(blocks[mode === 'light' ? 0 : 1]).toContain('--dipsy-intent-measurement-main: ' + semanticIntents[mode].measurement.main + ';');
  });
  it('requires explicit contrastText rather than selecting a fallback', () => {
    const raw = fresh();
    delete at(raw, 'tier2.intents.light.action.primary').contrastText;
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/requires authored contrastText/);
  });
  it('rejects an adapter literal even when its value currently matches', () => {
    const raw = fresh();
    at(raw, 'tier2.colorSchemes.light.primary').main = semanticIntents.light.action.primary.main;
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/adapter must reference/);
  });
  it('rejects semantic substitutions even when they share coordinates', () => {
    const raw = fresh();
    at(raw, 'tier2.colorSchemes.light.primary').main = '{tier2.intents.light.measurement.main}';
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/adapter must reference/);
    const swapped = fresh();
    at(swapped, 'tier2._intentContracts.measurement').semantic = 'action.primary';
    expect(() => generateThemeSource(JSON.stringify(swapped))).toThrow(/mismatched intent contract/);
  });
  it('keeps financial reserved, independent of chart opportunity, and out of all generated outputs', () => {
    expect(generateThemeSource(source)).not.toMatch(/"financial"/);
    expect(generateStaticTokensCss(source)).not.toContain('-financial-');
    const raw = fresh();
    at(raw, 'tier2._intentContracts.financial').stability = 'stable';
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/financial must remain reserved/);
    at(raw, 'tier2._intentContracts.financial').stability = 'reserved';
    at(raw, 'tier2.intents.light.financial').main = '{tier2.intents.light.chart.opportunity}';
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/financial|does not allow/);
  });
  it('does not allow a public intent to disappear through a lifecycle change', () => {
    const raw = fresh();
    at(raw, 'tier2._intentContracts.measurement').stability = 'reserved';
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/public intents must remain stable/);
  });
  it('rejects an indirect reserved-family alias in component tokens', () => {
    const raw = fresh();
    at(raw, 'tier3').alias = '{tier2.intents.light.financial.main}';
    at(raw, 'tier3').amount = '{tier3.alias}';
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/financial tokens are reserved/);
  });
});

describe('production consumer boundaries', () => {
  it('checks current React, design previews, popup and setup sources', () => {
    expect(() => validateRepositoryConsumers(path.resolve('..'))).not.toThrow();
  });
  it.each([
    'const value = theme.vars.palette.financial.main;',
    'const value = theme.palette["financial"].main;',
    'const { financial: money } = palette;',
    'const sx = { color: "financial.main" };',
    'const value = <Button color="financial" />;',
    'const sx = { color: "var(--bridge-palette-financial-main)" };',
    'const sx = { color: "var(--dipsy-intent-financial-main)" };',
  ])('rejects reserved references: %s', (code) => {
    expect(() => validateConsumerSource(code, 'Component.tsx')).toThrow(/reserved/);
  });
  it.each(['colorjs.io', '../theme/color-validation', '../theme/intent-contracts.ts', '../theme/generate-theme', '../theme/tokens.json'])('rejects build-only runtime import %s', (module) => {
    expect(() => validateConsumerSource('import value from "' + module + '";', 'Component.tsx')).toThrow(/build-only/);
  });
  it('rejects framework-derived tones and non-OKLCH mixing', () => {
    expect(() => validateConsumerSource('const x = palette.augmentColor({ color });', 'Theme.ts')).toThrow(/augmentColor/);
    expect(() => validateConsumerSource('.x { color: color-mix(in srgb, red, blue); }', 'surface.css')).toThrow(/OKLCH/);
  });
  it('allows existing action, feedback and measurement consumers', () => {
    expect(() => validateConsumerSource('const sx = { color: "measurement.main", bgcolor: "action.selected" };', 'Component.tsx')).not.toThrow();
  });
});
