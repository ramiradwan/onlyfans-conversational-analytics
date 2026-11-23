// Build-only checks for rendered chart marks, persistent fields and reserved colors.
import Color from 'colorjs.io';

import { contrastRatio, contrastRatioOnComposite, parseColor, type RgbColor } from './color-validation.ts';
import { isOfferPricePath, type JsonObject } from './intent-contracts.ts';

type PairResult = { mode: string; foreground: string; background: string; minimum: number; ratio: number };
function object(root: JsonObject, path: string): JsonObject {
  return path.split('.').reduce((value, key) => value[key] as JsonObject, root);
}
function color(root: JsonObject, path: string): string {
  const keys = path.split('.');
  const parent = object(root, keys.slice(0, -1).join('.'));
  const value = parent[keys.at(-1)!];
  if (typeof value !== 'string') throw new Error('Missing color role: ' + path);
  return value;
}
function sameColor(left: RgbColor, right: RgbColor): boolean {
  return (['r', 'g', 'b', 'a'] as const).every((key) => Math.abs(left[key] - right[key]) < 0.00001);
}

export function qualifyThemeColors(root: JsonObject) {
  const pairs: PairResult[] = [];
  const distances: { mode: string; first: string; second: string; deltaEOK: number }[] = [];
  const modes = ['light', 'dark'];
  const offer = object(root, 'tier3.offerPrice');
  if (!offer || Object.keys(offer).length !== modes.length) throw new Error('Expected both offer price schemes');
  const reserved = ['main', 'light', 'dark', 'ink'].map(role => parseColor(color(root, 'tier1.colorFamily.financial.' + role)));
  const offerColors = modes.flatMap(mode => {
    const scheme = object(offer, mode);
    if (!scheme || Object.keys(scheme).length !== 2) throw new Error('Expected offer price ink and fill for ' + mode);
    const colors = ['ink', 'fill'].map(role => parseColor(color(offer, mode + '.' + role)));
    if (colors.some(value => value.a !== 1)) throw new Error(mode + ': offer price ink and fill must be opaque');
    if (colors.some(value => reserved.some(entry => sameColor(value, entry)))) {
      throw new Error(mode + '.offerPrice: financial field color is reserved');
    }
    return colors;
  });
  function checkScope(value: unknown, path: string): void {
    if (typeof value === 'string') {
      const local = isOfferPricePath(path);
      for (const literal of value.match(/oklch\([^)]*\)/gi) ?? []) {
        const parsed = parseColor(literal);
        if (path.startsWith('tier2.intents.') && reserved.some(entry => sameColor(parsed, entry))) {
          throw new Error(path + ': financial field color is reserved');
        }
        if (!local && offerColors.some(entry => sameColor(parsed, entry))) {
          throw new Error(path + ': offer price colors are local');
        }
      }
    } else if (value && typeof value === 'object') {
      for (const [key, child] of Object.entries(value)) checkScope(child, path ? path + '.' + key : key);
    }
  }
  checkScope(root, '');
  for (const mode of modes) {
    const intents = object(root, 'tier2.intents.' + mode);
    function requirePair(foreground: string, background: string, minimum: number, roles = intents): void {
      const ratio = contrastRatio(color(roles, foreground), color(roles, background));
      pairs.push({ mode, foreground, background, minimum, ratio });
      if (ratio < minimum) throw new Error(`${mode}.${foreground} on ${background} has ${ratio}:1 contrast; requires ${minimum}:1`);
    }
    requirePair('offerPrice.ink', 'offerPrice.fill', 11, { offerPrice: offer[mode] });
    requirePair('text.secondary', 'surface.paper', 4.5);
    if (color(intents, 'chart.opportunity') !== color(intents, 'measurement.main')) {
      throw new Error(mode + ': opportunity must use measurement jade');
    }
    const chartMarks = ['sentiment', 'volume', 'baseline', 'opportunity', 'positive', 'negative', 'neutral', 'unknown',
      ...Array.from({ length: 8 }, (_, index) => 'categorical' + (index + 1))];
    for (const role of chartMarks) requirePair('chart.' + role, 'surface.paper', 3);
    for (const role of ['positive', 'negative', 'neutral', 'unknown']) {
      requirePair('sentiment.' + role, 'chart.area', 3);
    }
    for (const role of ['positive', 'negative']) {
      requirePair('sentiment.' + role, 'communication.incomingSurface', 3);
      requirePair('sentiment.' + role, 'communication.outgoingSurface', 3);
    }
    const metrics = {
      measurement: 'measurement.main', sentiment: 'sentiment.positive',
      opportunity: 'measurement.main', connection: 'action.secondary.main',
    };
    for (const [field, foreground] of Object.entries(metrics)) {
      requirePair(foreground, 'surface.metric.' + field + '.fill', 3);
    }
    requirePair('feedback.error.main', 'surface.error', 3);
    requirePair('measurement.main', 'surface.paper', 4.5);
    if (color(intents, 'action.state.focus') !== color(intents, 'action.state.hover')) throw new Error(mode + ': focus must use the authored hover fill');
    requirePair('measurement.dark', 'surface.paper', 4.5);
    for (const tone of ['info', 'success', 'warning', 'error']) {
      requirePair('text.primary', 'surface.feedback.' + tone + '.fill', 4.5);
      requirePair('text.secondary', 'surface.feedback.' + tone + '.fill', 4.5);
      requirePair('action.state.selectedForeground', 'surface.feedback.' + tone + '.fill', 4.5);
    }
    requirePair('surface.avatar.text', 'surface.avatar.fill', 4.5);
    requirePair('surface.trust.ink', 'surface.trust.fill', 3);
    requirePair('action.state.selectedForeground', 'action.state.selected', 4.5);
    for (const backdrop of ['surface.paper', 'surface.canvas']) {
      const ratio = contrastRatioOnComposite(color(intents, 'surface.tooltip.text'), color(intents, 'surface.tooltip.fill'), color(intents, backdrop));
      pairs.push({ mode, foreground: 'surface.tooltip.text', background: 'surface.tooltip.fill over ' + backdrop, minimum: 4.5, ratio });
      if (ratio < 4.5) throw new Error(mode + ': tooltip text contrast fails');
    }
    for (const field of ['action', 'surface']) {
      const checkNeutral = (value: unknown): void => {
        if (typeof value === 'string') {
          for (const literal of value.match(/oklch\([^)]*\)/gi) ?? []) {
            const [, chroma, hue] = new Color(literal).to('oklch').coords;
            if (chroma !== null && hue !== null && chroma > 0.02 && hue >= 270 && hue <= 300) {
              throw new Error(mode + '.' + field + ': violet leaked into neutral chrome');
            }
          }
        } else if (value && typeof value === 'object') Object.values(value).forEach(checkNeutral);
      };
      checkNeutral(intents[field]);
    }

    for (let first = 1; first <= 8; first++) {
      for (let second = first + 1; second <= 8; second++) {
        const a = 'chart.categorical' + first, b = 'chart.categorical' + second;
        distances.push({ mode, first: a, second: b,
          deltaEOK: new Color(color(intents, a)).deltaE(new Color(color(intents, b)), 'OK') });
      }
    }
  }
  return {
    schema: 'bridge-color-qualification/v1', pairs,
    diagnostics: {
      meaning: 'Palette distances are review aids, not accessibility scores.',
      distances,
      canvasOfferReference: {
        referenceOnly: true,
        ratio: contrastRatio('oklch(21.96% 0.0036 164.71)', 'oklch(84.4069% 0.16972 96.01)'),
      },
    },
  };
}
