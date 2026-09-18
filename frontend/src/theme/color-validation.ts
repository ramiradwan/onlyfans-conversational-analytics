// Build-time color validation. Never import this module from application code.
import Color from 'colorjs.io';

export type AllowedGamut = 'srgb' | 'display-p3';
export type RgbColor = { r: number; g: number; b: number; a: number; inSrgb: boolean };
// Preserve the existing generator's tolerance for rounded OKLCH coordinates.
export const LINEAR_GAMUT_EPSILON = 5e-4;

function parseCanonical(value: string): Color {
  const match = /^oklch\(\s*([+-]?[\d.]+%?)\s+([+-]?[\d.]+)\s+([+-]?[\d.]+)(?:deg)?(?:\s*\/\s*([+-]?[\d.]+%?))?\s*\)$/i.exec(value);
  if (!match) throw new Error('Design color tokens must use OKLCH: ' + value);
  const fraction = (s: string) => Number.parseFloat(s) / (s.endsWith('%') ? 100 : 1);
  const l = fraction(match[1]);
  const c = Number(match[2]);
  const h = Number(match[3]);
  const a = match[4] === undefined ? 1 : fraction(match[4]);
  if (![l, c, h, a].every(Number.isFinite) || l < 0 || l > 1 || c < 0 || a < 0 || a > 1) {
    throw new Error('OKLCH color component out of range: ' + value);
  }
  return new Color(value);
}

function inGamut(color: Color, gamut: AllowedGamut): boolean {
  return color.to(gamut === 'srgb' ? 'srgb-linear' : 'p3-linear').coords.every(
    (channel) => channel !== null && Number.isFinite(channel) && channel >= -LINEAR_GAMUT_EPSILON && channel <= 1 + LINEAR_GAMUT_EPSILON,
  );
}

export function assertGamut(value: string, gamut: AllowedGamut, path: string): void {
  if (!inGamut(parseCanonical(value), gamut)) {
    throw new Error(path + ' is outside the guaranteed ' + (gamut === 'srgb' ? 'sRGB' : 'Display-P3') + ' gamut');
  }
}

export function parseColor(value: string): RgbColor {
  const color = parseCanonical(value);
  const [r, g, b] = color.to('srgb').coords.map((channel) => {
    if (channel === null || !Number.isFinite(channel)) throw new Error('Invalid converted color: ' + value);
    return Math.min(1, Math.max(0, channel));
  });
  return { r, g, b, a: color.alpha, inSrgb: inGamut(color, 'srgb') };
}

/** Composite encoded sRGB channels, matching existing CSS surface validation. */
export function flattenOverBackground(value: string, backdrop: string | RgbColor): RgbColor {
  const color = parseColor(value);
  const bg = typeof backdrop === 'string' ? parseColor(backdrop) : backdrop;
  if (bg.a < 1) throw new Error('Contrast backdrop must be opaque');
  return {
    r: color.a * color.r + (1 - color.a) * bg.r,
    g: color.a * color.g + (1 - color.a) * bg.g,
    b: color.a * color.b + (1 - color.a) * bg.b,
    a: 1,
    inSrgb: color.inSrgb && bg.inSrgb,
  };
}

function contrast(a: RgbColor, b: RgbColor): number {
  return new Color('srgb', [a.r, a.g, a.b]).contrastWCAG21(new Color('srgb', [b.r, b.g, b.b]));
}

export function contrastRatio(foreground: string, backdrop: string): number {
  const bg = parseColor(backdrop);
  return contrast(flattenOverBackground(foreground, bg), bg);
}

export function contrastRatioOnComposite(foreground: string, overlay: string, backdrop: string): number {
  const bg = flattenOverBackground(overlay, backdrop);
  return contrast(flattenOverBackground(foreground, bg), bg);
}
