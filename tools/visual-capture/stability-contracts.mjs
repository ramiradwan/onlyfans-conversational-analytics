// Limits and grading for first-paint color and layout-shift measurements.

/** Layout Instability score per scenario. Chromium reports only moves of at least 3 CSS px. */
export const LAYOUT_SHIFT = { warn: 0, fail: 0.01 };

/**
 * Layout Instability score when web fonts arrive after text paints, so the fallback faces swap out.
 * Metric-matched fallbacks keep this near zero; the warn limit marks their metrics drifting.
 */
export const FONT_SWAP_SHIFT = { warn: 0.001, fail: 0.01 };

/** OKLab distance between the canvas painted before the app script runs and the app's canvas. */
export const CANVAS_DELTA = { warn: 0.001, fail: 0.02 };

/** Elements whose movement fails a scenario at any score. */
export const KEY_ELEMENTS = 'h1, header, nav, [data-visual="brand-tile"], .MuiButton-contained';

/** Returns 'fail' at or above the fail limit, 'warn' above the warn limit, otherwise 'pass'. */
export function grade(value, { warn, fail }) {
  if (value >= fail) return 'fail';
  if (value > warn) return 'warn';
  return 'pass';
}

/** Grades layout-shift entries recorded without recent input; any moved key element fails. */
export function gradeLayoutShifts(entries, limits = LAYOUT_SHIFT) {
  const counted = entries.filter((entry) => !entry.hadRecentInput);
  const score = counted.reduce((sum, entry) => sum + entry.value, 0);
  const keyMoves = counted.flatMap((entry) => entry.sources.filter((source) => source.key));
  const level = keyMoves.length > 0 ? 'fail' : grade(score, limits);
  return { score: Number(score.toPrecision(3)), level, keyMoves, moved: counted.flatMap((entry) => entry.sources) };
}

const srgbToLinear = (channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);

function linearSrgbToOklab([r, g, b]) {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

const number = (token, percentScale = 1) => {
  if (token === 'none') return 0;
  return token.endsWith('%') ? (Number.parseFloat(token) / 100) * percentScale : Number.parseFloat(token);
};

/** Parses a computed CSS color into OKLab coordinates and alpha. */
export function parseColor(value) {
  const match = /^(rgba?|oklch|oklab|color)\((.*)\)$/.exec(value.trim());
  if (!match) throw new Error(`unsupported color: ${value}`);
  const [, fn, body] = match;
  const [channels, alphaToken] = body.includes('/') ? body.split('/') : [body, null];
  const tokens = channels.trim().split(/[\s,]+/);
  let alpha = alphaToken === null ? 1 : number(alphaToken.trim());
  if (fn === 'rgb' || fn === 'rgba') {
    if (tokens.length === 4) alpha = number(tokens.pop());
    return { lab: linearSrgbToOklab(tokens.map((token) => srgbToLinear(number(token, 255) / 255))), alpha };
  }
  if (fn === 'color') {
    if (tokens.shift() !== 'srgb') throw new Error(`unsupported color space: ${value}`);
    return { lab: linearSrgbToOklab(tokens.map((token) => srgbToLinear(number(token)))), alpha };
  }
  if (fn === 'oklab') return { lab: [number(tokens[0]), number(tokens[1], 0.4), number(tokens[2], 0.4)], alpha };
  const [lightness, chroma, hue] = [number(tokens[0]), number(tokens[1], 0.4), number(tokens[2]) * (Math.PI / 180)];
  return { lab: [lightness, chroma * Math.cos(hue), chroma * Math.sin(hue)], alpha };
}

/** Euclidean OKLab distance between two opaque computed colors. */
export function colorDistance(first, second) {
  const [a, b] = [parseColor(first), parseColor(second)];
  if (a.alpha < 1 || b.alpha < 1) throw new Error(`canvas colors must be opaque: ${first}, ${second}`);
  return Math.hypot(...a.lab.map((channel, index) => channel - b.lab[index]));
}
