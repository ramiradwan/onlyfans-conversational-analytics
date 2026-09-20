import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { contrastRatio } from '../src/theme/color-validation';
import { generateThemeSource, generateStaticTokensCss, replaceStaticTokenBlock, staticTokenConsumers } from '../src/theme/generate-theme';
import { semanticIntents } from '../src/theme/generated/tokens';
import { generateStaticFontsCss, replaceStaticFontPreloads } from '../src/theme/static-fonts';

const root = path.resolve('..');
const read = (file: string) => fs.readFileSync(path.join(root, file), 'utf8').replace(/\r\n/g, '\n');
const sha = (text: string | Buffer) => createHash('sha256').update(text).digest('hex');
const fonts = generateStaticFontsCss(root);
const baseline = JSON.parse(read('frontend/tests/fixtures/static-surface-content.json')) as {
  sha256: Record<string, string>; provisioningBodyText: string;
};

describe('static surface font delivery', () => {
  it('generates the same font bytes and measured fallbacks deterministically', () => {
    expect(generateStaticFontsCss(root)).toBe(fonts);
    expect(read('frontend/src/theme/generated/static-fonts.css')).toBe(fonts);
    const source = read('frontend/src/index.css');
    for (const [face] of source.matchAll(/@font-face\s*\{[^}]*\}/g)) expect(fonts).toContain(face);
  });
  it.each(staticTokenConsumers)('embeds the generated local fonts in %s', (file) => {
    expect(replaceStaticTokenBlock(read(file), fonts, 'static-fonts')).toBe(read(file));
  });
  it('ships only the pinned latin WOFF2 faces and their redistribution notices', () => {
    const urls = [...fonts.matchAll(/url\(([^)]+)\)/g)].map((match) => match[1]);
    expect(urls).toHaveLength(2);
    const originals = [
      '@fontsource-variable/inter/files/inter-latin-opsz-normal.woff2',
      '@fontsource-variable/space-grotesk/files/space-grotesk-latin-wght-normal.woff2',
    ];
    urls.forEach((url, index) => {
      expect(url.startsWith('data:font/woff2;base64,')).toBe(true);
      const bytes = Buffer.from(url.split(',')[1], 'base64');
      expect(bytes.subarray(0, 4).toString()).toBe('wOF2');
      expect(sha(bytes)).toBe(sha(fs.readFileSync(path.resolve('node_modules', originals[index]))));
    });
    expect(fonts).not.toContain('@import');
    expect(fonts.match(/SIL OPEN FONT LICENSE/g)?.length).toBeGreaterThanOrEqual(2);
  });
});

it.each([['extension/popup.html', 2], ['extension/setup.html', 2], ['extension/options.html', 2], ['app/provisioning/provisioning.html', 1]] as const)('keeps early font preloads current in %s', (file, count) => {
  expect(replaceStaticFontPreloads(read(file), fonts, count)).toBe(read(file));
});

describe('static surface content and behavior boundaries', () => {
  it('preserves the bound risk disclosure while operational copy changes', () => {
    const file = 'app/provisioning/creator-platform-data-risk-disclosure.html';
    expect(sha(read(file))).toBe(baseline.sha256[file]);
  });
  it('keeps setup branding, guidance and the authoritative step order', () => {
    const doc = new DOMParser().parseFromString(read('app/provisioning/provisioning.html'), 'text/html');
    expect(doc.querySelector('body > header.site-brand')).not.toBeNull();
    expect([...doc.querySelectorAll('[data-step]')].map((node) => node.getAttribute('data-step')))
      .toEqual(['registration', 'identity', 'approval', 'finalization']);
    expect(doc.querySelectorAll('link')).toHaveLength(1);
    expect(doc.querySelector('link')?.getAttribute('href')).toMatch(/^data:font\/woff2;base64,/);
    expect(doc.querySelector('#full-disclosure')).toBeNull();
    expect(doc.querySelector('textarea')?.getAttribute('aria-describedby'))
      .toBe('claim-package-help claim-package-validation claim-package-count');
  });
});

describe('static semantic treatments', () => {
  it('rejects primary-colored secondary text on tinted feedback fields', () => {
    const raw = JSON.parse(read('frontend/src/theme/tokens.json'));
    raw.tier2.intents.light.action.state.selectedForeground = raw.tier1.colorFamily.measurementJade.light.main;
    expect(() => generateThemeSource(JSON.stringify(raw))).toThrow(/selectedForeground on surface.feedback.*contrast/);
  });
  it('derives setup header geometry from the existing shell', () => {
    const raw = JSON.parse(read('frontend/src/theme/tokens.json'));
    raw.tier1.layout.shell.headerHeight = 80;
    raw.tier3.shell.railInset = 16;
    const css = generateStaticTokensCss(JSON.stringify(raw));
    expect(css).toContain('--dipsy-static-header-height: 80px;');
    expect(css).toContain('--dipsy-static-brand-inset: 32px;');
  });

  it.each(['light', 'dark'] as const)('keeps %s feedback fields opaque and body text readable', (mode) => {
    const { surface, text } = semanticIntents[mode];
    for (const tone of Object.values(surface.feedback)) {
      expect(tone.fill).not.toContain('/');
      expect(contrastRatio(text.primary, tone.fill)).toBeGreaterThanOrEqual(4.5);
    }
  });
  it('keeps steady success on paper, and uses named warning/error fields instead of hue mixing', () => {
    const css = read('extension/popup.css').split('/* design-tokens:end */')[1];
    expect(css).toContain('.journey-card[data-tone="success"] { background: var(--dipsy-color-paper); }');
    expect(css).toContain('var(--dipsy-intent-surface-feedback-warning-fill)');
    expect(css).toContain('var(--dipsy-intent-surface-feedback-error-fill)');
    expect(css).not.toContain('color-mix(');
    expect(css).toContain('font-family: var(--dipsy-font-family-numeric)');
  });
});
