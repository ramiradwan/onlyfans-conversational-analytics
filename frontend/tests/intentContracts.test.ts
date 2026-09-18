import { describe, expect, it } from 'vitest';
import { buildTokenGraph, publicIntents, type JsonObject } from '../src/theme/intent-contracts';
import { assertGamut, contrastRatio, contrastRatioOnComposite, parseColor } from '../src/theme/color-validation';

function palette(semantic = 'measurement'): JsonObject {
  return {
    _intent: {
      semantic, kind: 'palette', allowedGamut: 'srgb',
      usage: { allow: [semantic], deny: [] },
      accessibility: { contrastTextMin: 4.5, colorIndependentMeaning: true },
      derived: { scope: 'none', colorSpace: 'oklch', operations: [] },
      provenance: { owner: 'Bridge', decision: 'intent-contracts', introduced: '2026-09' },
      stability: semantic === 'financial.value' ? 'reserved' : 'stable',
    },
    main: 'oklch(55.5912% 0.22480 277.32)',
    light: 'oklch(68.2072% 0.17271 282.31)',
    dark: 'oklch(46.7674% 0.20489 275.95)',
    contrastText: 'oklch(100% 0 0)',
  };
}
function source(measurement: JsonObject = palette()): JsonObject {
  return { tier2: { intents: { light: { measurement, financial: palette('financial.value') } } } };
}
const policy = (p: JsonObject) => p._intent as JsonObject;

describe('intent contracts before metadata removal', () => {
  it('resolves values deterministically and excludes reserved runtime families', () => {
    const raw = source();
    const first = buildTokenGraph(raw);
    expect(first.resolved).toEqual(buildTokenGraph(raw).resolved);
    expect(JSON.stringify(first.resolved)).not.toContain('_intent');
    expect(JSON.stringify(raw)).toContain('_intent');
    expect(first.policies.size).toBe(2);
    expect(publicIntents(first).light).not.toHaveProperty('financial');
  });
  it('validates shared policy references before metadata is stripped', () => {
    const p = palette();
    const raw = source(p);
    raw._policies = { measurement: p._intent };
    p._intent = '{_policies.measurement}';
    expect(buildTokenGraph(raw).policies.get('tier2.intents.light.measurement')?.semantic).toBe('measurement');
    (raw._policies as JsonObject).measurement = '{_policies.measurement}';
    expect(() => buildTokenGraph(raw)).toThrow(/Circular intent policy/);
    p._intent = '{_policies.missing}';
    expect(() => buildTokenGraph(raw)).toThrow(/Unknown intent policy/);
  });
  it.each(['main', 'light', 'dark', 'contrastText'])('requires authored %s', (field) => {
    const p = palette(); delete p[field];
    expect(() => buildTokenGraph(source(p))).toThrow(/requires authored/);
  });
  it.each(['semantic', 'kind', 'allowedGamut', 'usage', 'accessibility', 'derived', 'provenance', 'stability'])('requires %s metadata', (field) => {
    const p = palette(); delete policy(p)[field];
    expect(() => buildTokenGraph(source(p))).toThrow(/invalid _intent/);
  });
  it('rejects unknown metadata rather than silently discarding policy', () => {
    const p = palette(); policy(p).allowP3 = true;
    expect(() => buildTokenGraph(source(p))).toThrow(/invalid _intent/);
  });
  it('rejects essential wide-gamut policy', () => {
    const p = palette(); policy(p).allowedGamut = 'display-p3';
    expect(() => buildTokenGraph(source(p))).toThrow(/essential intents/);
  });
  it('rejects policy with disabled but populated derivations', () => {
    const p = palette(); (policy(p).derived as JsonObject).operations = ['alpha'];
    expect(() => buildTokenGraph(source(p))).toThrow(/derivation scope/);
  });
  it('rejects undeclared intent values and runtime-derived durable colors', () => {
    expect(() => buildTokenGraph(source({ main: 'oklch(50% 0 0)' }))).toThrow(/ungoverned/);
    const p = palette(); p.light = 'color-mix(in oklch, white, black)';
    expect(() => buildTokenGraph(source(p))).toThrow(/runtime derivation/);
  });
  it('rejects invalid contrast and color aliases', () => {
    const p = palette(); p.contrastText = p.main;
    expect(() => buildTokenGraph(source(p))).toThrow(/contrast/);
    p.contrastText = 'oklch(100% 0 0)'; p.light = '{missing.color}';
    expect(() => buildTokenGraph(source(p))).toThrow(/Unknown token reference/);
  });
  it('tracks indirect and object-valued financial aliases', () => {
    for (const target of ['tier2.intents.light.financial.main', 'tier2.intents.light.financial']) {
      const raw = source(); raw.alias = '{' + target + '}'; raw.consumer = '{alias}';
      expect(() => buildTokenGraph(raw)).toThrow(/financial tokens are reserved/);
    }
    const raw = source();
    raw.tier1 = { colorFamily: { financial: { main: 'oklch(84.4069% 0.16972 96.01)' } } };
    raw.consumer = '{tier1.colorFamily.financial.main}';
    expect(() => buildTokenGraph(raw)).toThrow(/financial tokens are reserved/);
  });
  it('enforces allow and deny rules across semantic references', () => {
    const raw = source(); raw.consumer = '{tier2.intents.light.measurement.main}';
    expect(() => buildTokenGraph(raw)).toThrow(/does not allow consumer component/);
    const p = palette(); (policy(p).usage as JsonObject).deny = ['component'];
    (policy(p).usage as JsonObject).allow = ['measurement', 'component'];
    expect(() => buildTokenGraph({ ...source(p), consumer: '{tier2.intents.light.measurement.main}' })).toThrow(/does not allow/);
  });
  it('requires color-independent meaning for essential roles', () => {
    const p = palette();
    (policy(p).accessibility as JsonObject).colorIndependentMeaning = false;
    expect(() => buildTokenGraph(source(p))).toThrow(/color-independent/);
  });
  it('enforces declared non-text pairs and rejects translucent backdrops', () => {
    const p = palette();
    (policy(p).accessibility as JsonObject).nonText = { against: ['{backdrop}'], minimum: 3 };
    expect(() => buildTokenGraph({ ...source(p), backdrop: 'oklch(100% 0 0)' })).not.toThrow();
    expect(() => buildTokenGraph({ ...source(p), backdrop: p.main })).toThrow(/non-text contrast/);
    expect(() => buildTokenGraph({ ...source(p), backdrop: 'oklch(100% 0 0 / 0.5)' })).toThrow(/opaque/);
  });
  it('validates colors embedded in effects, not only palette fields', () => {
    expect(() => buildTokenGraph({ effect: 'linear-gradient(0deg, oklch(60% 0.4 30), oklch(100% 0 0))' })).toThrow(/sRGB/);
  });
  it('does not let a financial-family reference hide behind a different alias name', () => {
    const raw = source();
    raw.bridge = { value: '{tier2.intents.light.financial.main}' };
    raw.consumer = '{bridge.value}';
    expect(() => buildTokenGraph(raw)).toThrow(/financial tokens are reserved/);
  });
  it('resolves object-alias children and rejects cycles', () => {
    const raw = { color: { main: 'oklch(50% 0 0)' }, alias: '{color}', child: '{alias.main}' };
    expect(buildTokenGraph(raw).resolved.child).toBe('oklch(50% 0 0)');
    expect(() => buildTokenGraph({ a: '{b}', b: '{a}' })).toThrow(/Circular/);
    expect(() => buildTokenGraph({ a: '{b}', b: '{a.color}' })).toThrow(/Circular/);
  });
});

describe('build-time color science', () => {
  it('preserves rounded baseline gamut tolerance without accepting wide colors', () => {
    const rounded = 'oklch(52.8516% 0.17984 142.50)';
    expect(parseColor(rounded).inSrgb).toBe(true);
    expect(() => assertGamut(rounded, 'srgb', 'feedback.success')).not.toThrow();
    expect(() => assertGamut('oklch(60% 0.4 30)', 'srgb', 'test')).toThrow(/sRGB gamut/);
    expect(() => assertGamut('oklch(60% 0.5 30)', 'display-p3', 'test')).toThrow(/Display-P3 gamut/);
  });
  it('measures luminance after compositing translucent text and surfaces', () => {
    expect(contrastRatio('oklch(0% 0 0)', 'oklch(100% 0 0)')).toBeCloseTo(21, 8);
    expect(contrastRatio('oklch(0% 0 0 / 0.5)', 'oklch(100% 0 0)')).toBeCloseTo(3.97665, 4);
    expect(contrastRatioOnComposite('oklch(0% 0 0)', 'oklch(100% 0 0 / 0.5)', 'oklch(0% 0 0)')).toBeCloseTo(5.28082, 4);
    expect(() => contrastRatio('oklch(0% 0 0)', 'oklch(100% 0 0 / 0.5)')).toThrow(/opaque/);
  });
  it.each(['oklch(110% 0 0)', 'oklch(50% -0.1 0)', 'oklch(50% 0 0 / 2)', 'oklch(50% 0 none)'])('rejects invalid authored color %s', (value) => {
    expect(() => parseColor(value)).toThrow();
  });
});
