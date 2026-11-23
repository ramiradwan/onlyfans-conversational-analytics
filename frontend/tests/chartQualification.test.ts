import fs from 'node:fs';
import { describe, expect, it } from 'vitest';

import { generateColorReport, generateThemeSource } from '../src/theme/generate-theme';
import { semanticIntents } from '../src/theme/generated/tokens';
import { validateConsumerSource } from '../src/theme/token-consumers';

const source = fs.readFileSync('src/theme/tokens.json', 'utf8');
const fresh = () => JSON.parse(source);
const offerPrice = () => ({
  light: { fill: 'oklch(87% 0.15 96)', ink: 'oklch(18% 0.008 95)' },
  dark: { fill: 'oklch(88% 0.14 96)', ink: 'oklch(19% 0.008 95)' },
});

describe('analytical color qualification', () => {
  it('reports deterministic measured pairs separately from diagnostic palette distances', () => {
    const report = generateColorReport(source);
    expect(generateColorReport(source)).toBe(report);
    const result = JSON.parse(report);
    expect(result.schema).toBe('bridge-color-qualification/v1');
    expect(result.pairs).toHaveLength(100);
    for (const mode of ['light', 'dark']) {
      expect(result.pairs).toContainEqual(expect.objectContaining({
        mode, foreground: 'offerPrice.ink', background: 'offerPrice.fill', minimum: 11,
      }));
      expect(result.pairs).toContainEqual(expect.objectContaining({
        mode, foreground: 'text.secondary', background: 'surface.paper', minimum: 4.5,
      }));
    }
    expect(result.pairs.every((pair: { ratio: number; minimum: number }) => pair.ratio >= pair.minimum)).toBe(true);
    expect(result.diagnostics.distances).toHaveLength(56);
    expect(result.diagnostics.meaning).toContain('not accessibility scores');
  });
  it.each(['light', 'dark'] as const)('rejects a below-11:1 %s offer pair', (mode) => {
    const raw = fresh();
    raw.tier3.offerPrice = offerPrice();
    raw.tier3.offerPrice[mode].ink = 'oklch(27% 0.008 95)';
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/offerPrice\.ink on offerPrice\.fill.*requires 11:1/);
  });
  for (const mode of ['light', 'dark'] as const) {
    it(`rejects ${mode} warning use of the offer fill literal`, () => {
      const raw = fresh();
      raw.tier3.offerPrice = offerPrice();
      raw.tier2.intents[mode].surface.feedback.warning.fill = raw.tier3.offerPrice[mode].fill;
      expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/warning\.fill: offer price colors are local/);
    });
    it(`rejects ${mode} opportunity use of the offer fill literal`, () => {
      const raw = fresh();
      raw.tier3.offerPrice = offerPrice();
      raw.tier2.intents[mode].chart.opportunity = raw.tier3.offerPrice[mode].fill;
      expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/chart\.opportunity: offer price colors are local/);
    });
  }
  it.each(['light', 'dark'] as const)('rejects the reserved literal in the %s offer', (mode) => {
    const raw = fresh();
    raw.tier3.offerPrice = offerPrice();
    raw.tier3.offerPrice[mode].fill = raw.tier1.colorFamily.financial.main;
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/financial field color is reserved/);
  });
  it.each(['light', 'dark'] as const)('makes %s opportunity nonfinancial and keeps the financial family out of output', (mode) => {
    expect(semanticIntents[mode].chart.opportunity).toBe(semanticIntents[mode].measurement.main);
    expect(generateThemeSource(source)).not.toContain('"financial"');
    const raw = fresh();
    raw.tier2.intents[mode].chart.opportunity = raw.tier1.colorFamily.financial.main;
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/financial field color is reserved/);
  });
  it('rejects an invisible neutral mark without confusing it with a quiet grid', () => {
    const raw = fresh();
    raw.tier2.intents.dark.sentiment.neutral = raw.tier1.colorFamily.neutralWarm.dark.paper;
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/chart.neutral.*contrast/);
  });
  it('rejects unreadable persistent icon fields', () => {
    const raw = fresh();
    raw.tier2.intents.light.surface.metric.measurement.fill = raw.tier1.colorFamily.measurementJade.light.main;
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/measurement.main on surface.metric.measurement.fill/);
  });
  it('keeps persistent analytical surfaces on named tokens', () => {
    for (const file of ['MetricCard.tsx', 'AnalyticsStateFrame.tsx', 'SentimentEngagementTrend.tsx']) {
      expect(fs.readFileSync('src/components/analytics/' + file, 'utf8')).not.toContain('color-mix(');
    }
    const tone = fs.readFileSync('src/components/inbox/MessageTone.tsx', 'utf8');
    expect(tone).toContain('theme.vars.palette.sentiment.positive');
    expect(tone).not.toContain('theme.vars.palette.success');
  });
  it('keeps color-qualification code out of runtime imports', () => {
    expect(() => validateConsumerSource('import value from "../theme/chart-qualification";', 'Component.tsx')).toThrow(/build-only/);
  });
  it.each(['const c = theme.vars.palette.accent.main;', 'const sx = { color: "calm.main" };'])(
    'rejects new consumers of deprecated aliases: %s', (code) => {
      expect(() => validateConsumerSource(code, 'Component.tsx')).toThrow(/deprecated palette alias/);
    },
  );

});
