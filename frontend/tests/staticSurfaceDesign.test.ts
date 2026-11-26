import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { contrastRatio } from '../src/theme/color-validation';
import { generateNativeOnboardingBundle, generateThemeSource, generateStaticTokensCss, replaceStaticTokenBlock, staticTokenConsumers } from '../src/theme/generate-theme';
import { semanticIntents } from '../src/theme/generated/tokens';
import { generateStaticFontsCss, replaceStaticFontPreloads } from '../src/theme/static-fonts';

const root = path.resolve('..');
const read = (file: string) => fs.readFileSync(path.join(root, file), 'utf8').replace(/\r\n/g, '\n');
const sha = (text: string | Buffer) => createHash('sha256').update(text).digest('hex');
const fonts = generateStaticFontsCss(root);
const tokensSource = read('frontend/src/theme/tokens.json');
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

describe('native surface font delivery', () => {
  it('packages the same deterministic CSS, pinned Inter bytes and redistribution notice', () => {
    expect(typeof generateNativeOnboardingBundle).toBe('function');
    const bundle = generateNativeOnboardingBundle(tokensSource, root);
    expect(generateNativeOnboardingBundle(tokensSource, root)).toEqual(bundle);
    const pinned = 'frontend/node_modules/@fontsource-variable/inter';
    const file = 'inter-latin-opsz-normal.woff2';
    expect(bundle.font.filename).toBe(file);
    expect(sha(bundle.font.bytes)).toBe(sha(fs.readFileSync(path.join(root, pinned, 'files', file))));
    expect(bundle.font.bytes.subarray(0, 4).toString()).toBe('wOF2');
    expect(sha(fs.readFileSync(path.join(root, 'desktop/shell', file)))).toBe(sha(bundle.font.bytes));
    expect(read('desktop/shell/onboarding-theme.css')).toBe(bundle.css);
    expect(bundle.css).toContain(read(pinned + '/LICENSE').trim());
    expect(bundle.css).toContain('sha256:' + sha(bundle.font.bytes));
    expect(bundle.css).toMatch(/@font-face\s*\{[^}]*font-family:\s*'Inter Variable'[^}]*src:\s*url\(\.\/inter-latin-opsz-normal\.woff2\)/);
    expect(bundle.css).not.toMatch(/url\(\s*['"]?(?:https?:|data:font)|@import/);
  });

  it('allows same-origin fonts through the otherwise identical native CSP', () => {
    const { build, app } = JSON.parse(read('desktop/src-tauri/tauri.conf.json'));
    expect(build.frontendDist).toBe('../shell');
    const { csp, capabilities } = app.security;
    expect(csp.split('; ')).toEqual([
      "default-src 'none'", "style-src 'self'", "script-src 'self'", "img-src 'self'",
      'connect-src ipc: http://ipc.localhost', "font-src 'self'",
    ]);
    expect(capabilities).toEqual(['desktop', 'window-frame', 'account']);
    const account = JSON.parse(read('desktop/src-tauri/capabilities/account.json'));
    expect(account.windows).toEqual(['main']);
    expect(account.platforms).toEqual(['windows']);
    expect(account.local).toBe(false);
    expect(account.remote).toEqual({ urls: ['http://bridge.localhost:17871/account'] });
    expect(account.permissions).toEqual(['allow-desktop-account-passkeys']);
  });

  it('packages each native mask as the same outline SVG at a relative URL', () => {
    const bundle = generateNativeOnboardingBundle(tokensSource, root);
    const layout = read('frontend/src/theme/onboarding.css');
    const masks = [...layout.matchAll(/^\.onboarding-icon\[data-icon="([a-z-]+)"\].*?url\("data:image\/svg\+xml,([^"]+)"\)/gm)];
    expect(masks).toHaveLength(19);
    expect(bundle.css).not.toContain('data:image/');
    expect(fs.readdirSync(path.join(root, 'desktop/shell/onboarding-icons')).sort())
      .toEqual(masks.map(([, name]) => name + '.svg').sort());
    for (const [, name, svg] of masks) {
      expect(bundle.css).toContain(`url("./onboarding-icons/${name}.svg")`);
      expect(read(`desktop/shell/onboarding-icons/${name}.svg`)).toBe(decodeURIComponent(svg) + '\n');
    }
  });
});

describe('opt-in static onboarding primitives', () => {
  const css = read('frontend/src/theme/onboarding.css');

  it('pairs 24-grid masks with the installed outlined paths and uses currentColor', () => {
    expect(css).toContain('Apache-2.0');
    for (const icon of ['KeyOutlined', 'WebAssetOutlined', 'CheckCircleOutlined', 'ErrorOutlineOutlined']) {
      const source = read('frontend/node_modules/@mui/icons-material/' + icon + '.mjs');
      const pathData = /\bd:\s*"([^"]+)"/.exec(source)?.[1];
      expect(pathData, icon).toBeTruthy();
      expect(css).toContain(encodeURIComponent(pathData!));
    }
    expect(css).toContain('viewBox%3D%220%200%2024%2024%22');
    expect(css).toMatch(/\.onboarding-icon\[data-icon\]\s*\{[^}]*background:\s*currentColor/);
  });

  it('uses the authored timing, has no animated layout, and stops in reduced motion', () => {
    const primitives = css.slice(css.indexOf('.onboarding-surface {'));
    expect(primitives).toMatch(/--onboarding-arrival:\s*200ms/);
    expect(primitives).toMatch(/--onboarding-swap:\s*180ms/);
    expect(primitives).toMatch(/--onboarding-value:\s*240ms/);
    expect(primitives).toMatch(/onboarding-pending 1s linear infinite/);
    const frames = [...primitives.matchAll(/@keyframes[^{]+\{([\s\S]*?)(?=\n\})/g)];
    expect(frames).toHaveLength(4);
    for (const [, rules] of frames) {
      const properties = [...rules.matchAll(/(?:[{;])\s*([\w-]+):/g)].map(([, name]) => name);
      expect(properties.length).toBeGreaterThan(0);
      expect(properties.every(name => name === 'transform' || name === 'opacity')).toBe(true);
    }
    expect(primitives).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*animation:\s*none/);
    expect(primitives).not.toMatch(/line-clamp|text-overflow|visibility:\s*hidden/);
  });
});

it.each([['extension/popup.html', 2], ['extension/setup.html', 2], ['extension/options.html', 2]] as const)('keeps early font preloads current in %s', (file, count) => {
  expect(replaceStaticFontPreloads(read(file), fonts, count)).toBe(read(file));
  expect(replaceStaticFontPreloads(read(file).replace(/\n/g, '\r\n'), fonts, count)).toBe(read(file));
});

describe('static surface content and behavior boundaries', () => {
  it('preserves the bound risk disclosure while operational copy changes', () => {
    const file = 'app/provisioning/creator-platform-data-risk-disclosure.html';
    expect(sha(read(file))).toBe(baseline.sha256[file]);
  });
  it('keeps setup branding, guidance and the authoritative step order', () => {
    const doc = new DOMParser().parseFromString(read('frontend/onboarding/index.html'), 'text/html');
    expect(doc.querySelector('body > header.setup-header')).not.toBeNull();
    expect([...doc.querySelectorAll('[data-step]')].map(node => node.getAttribute('data-step')))
      .toEqual(['browser-access', 'account-connection', 'finish-setup']);
    expect(doc.querySelector('desktop-window-controls')).not.toBeNull();
    expect(doc.querySelector('textarea')).toBeNull();

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
    expect(read('frontend/shared-setup/shell.css')).toMatch(
      /\.setup-header \{[^}]*min-height: var\(--dipsy-static-header-height\);[^}]*var\(--dipsy-static-brand-inset\)/);
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
