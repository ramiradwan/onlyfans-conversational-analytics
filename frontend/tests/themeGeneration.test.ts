import InitColorSchemeScript from '@mui/material/InitColorSchemeScript';
import fs from 'node:fs';
import path from 'node:path';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { brandTypography, colorSchemeProps, theme } from '../src/theme';
import {
  firstPaintConsumers,
  generateFirstPaintCss,
  generateStaticTokensCss,
  generateThemeSource,
  replaceStaticTokenBlock,
  staticTokenConsumers,
} from '../src/theme/generate-theme';

describe('theme token generation', () => {
  const tokenPath = path.resolve(process.cwd(), 'src/theme/tokens.json');
  const tokensSource = fs.readFileSync(tokenPath, 'utf8');

  it('is deterministic and matches the checked-in generated source', () => {
    const generatedPath = path.resolve(process.cwd(), 'src/theme/generated/tokens.ts');
    const first = generateThemeSource(tokensSource);
    const second = generateThemeSource(tokensSource);
    const checkedIn = fs.readFileSync(generatedPath, 'utf8').replace(/\r\n/g, '\n');

    expect(first).toBe(second);
    expect(first).toBe(checkedIn);
  });

  it('authors design colors in OKLCH instead of legacy RGB syntax', () => {
    expect(tokensSource).toContain('oklch(');
    expect(tokensSource).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(tokensSource).not.toMatch(/rgba?\(/i);
  });

  it('rejects legacy color syntax in the token authority', () => {
    const legacy = tokensSource.replace('oklch(55.5912% 0.22480 277.32)', '#5B57F2');
    expect(() => generateThemeSource(legacy)).toThrow(/must use OKLCH/i);
  });

  it('rejects core colors outside the guaranteed sRGB gamut', () => {
    const wide = tokensSource.replace(
      'oklch(55.5912% 0.22480 277.32)',
      'oklch(60% 0.4 30)',
    );
    expect(() => generateThemeSource(wide)).toThrow(/outside the guaranteed sRGB gamut/i);
  });

  it('rejects a primary cue that loses non-text contrast against the paper surface', () => {
    type Tone = { main: string; contrastText: string };
    const weak = JSON.parse(tokensSource) as {
      tier2: { intents: { light: { action: { primary: Tone } } } };
    };
    // Keep the text pair valid to isolate insufficient selection-cue contrast.
    weak.tier2.intents.light.action.primary.main = 'oklch(92% 0.01 186.65)';
    weak.tier2.intents.light.action.primary.contrastText = '{tier1.onColor.black87}';
    expect(() => generateThemeSource(JSON.stringify(weak))).toThrow(/primary\.main \(selection cue\).*3:1/i);
  });

  it('leads the brand font stack with the family the bundled stylesheet declares', () => {
    const tokens = JSON.parse(tokensSource) as {
      tier1: { brandTypography: { fontFamily: string } };
    };
    const leading = tokens.tier1.brandTypography.fontFamily.split(',')[0].trim().replace(/^"|"$/g, '');
    const entryCss = fs.readFileSync(path.resolve(process.cwd(), 'src/index.css'), 'utf8');
    const fontImport = /@import '([^']+)';/.exec(entryCss)?.[1];
    expect(fontImport).toBeDefined();
    const fontCss = fs.readFileSync(
      path.resolve(process.cwd(), 'node_modules', fontImport as string),
      'utf8',
    );
    const declared = new Set(
      Array.from(fontCss.matchAll(/font-family:\s*'([^']+)'/g), (match) => match[1]),
    );
    expect([...declared]).toEqual([leading]);
  });
});

describe('static surface design tokens', () => {
  const tokensSource = fs.readFileSync(path.resolve(process.cwd(), 'src/theme/tokens.json'), 'utf8');
  const css = generateStaticTokensCss(tokensSource);

  it('matches the checked-in static token stylesheet', () => {
    const checkedIn = fs
      .readFileSync(path.resolve(process.cwd(), 'src/theme/generated/static-tokens.css'), 'utf8')
      .replace(/\r\n/g, '\n');
    expect(generateStaticTokensCss(tokensSource)).toBe(css);
    expect(css).toBe(checkedIn);
  });

  it('emits native OKLCH rather than projecting static surfaces back to legacy RGB syntax', () => {
    expect(css).toContain('--dipsy-color-primary: oklch(');
    expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(css).not.toMatch(/rgba?\(/i);
  });

  it.each(staticTokenConsumers)('%s carries the current generated token block', (consumer) => {
    const source = fs
      .readFileSync(path.resolve(process.cwd(), '..', consumer), 'utf8')
      .replace(/\r\n/g, '\n');
    expect(replaceStaticTokenBlock(source, css)).toBe(source);
    expect(source).not.toContain('color-mix(in srgb');
  });

  it('detects a hand-edited token value inside the block', () => {
    const consumer = ['a {}', '  /* design-tokens:start */', '  /* design-tokens:end */', ''].join('\n');
    const synced = replaceStaticTokenBlock(consumer, css);
    const edited = synced.replace(
      /--dipsy-color-primary: [^;]+;/,
      '--dipsy-color-primary: oklch(50% 0.10 277.32);',
    );
    expect(edited).not.toBe(synced);
    expect(replaceStaticTokenBlock(edited, css)).toBe(synced);
  });

  it('refuses consumers without exactly one marker pair', () => {
    expect(() => replaceStaticTokenBlock('a {}', css)).toThrow(/marker pair/);
    const pair = '/* design-tokens:start */\n/* design-tokens:end */\n';
    expect(() => replaceStaticTokenBlock(pair + pair, css)).toThrow(/marker pair/);
  });

  it('uses the authored on-primary text for each scheme', () => {
    const [light, dark] = css.split('@media (prefers-color-scheme: dark)');
    expect(light).toContain('--dipsy-color-on-primary: oklch(100% 0 0);');
    expect(dark).toContain('--dipsy-color-on-primary: oklch(0% 0 0 / 0.87);');
  });
});

describe('first-paint head', () => {
  const tokensSource = fs.readFileSync(path.resolve(process.cwd(), 'src/theme/tokens.json'), 'utf8');
  const css = generateFirstPaintCss(tokensSource);
  const colorSchemeScript = renderToStaticMarkup(
    createElement(InitColorSchemeScript, { attribute: 'data-mui-color-scheme', ...colorSchemeProps }),
  ).replace(/^<script[^>]*>/, '<script>');

  it.each(firstPaintConsumers)('%s carries the current first-paint colors and color scheme script', (consumer) => {
    const source = fs
      .readFileSync(path.resolve(process.cwd(), '..', consumer), 'utf8')
      .replace(/\r\n/g, '\n');
    expect(replaceStaticTokenBlock(source, css, 'first-paint')).toBe(source);
    expect(source).toContain(colorSchemeScript);
  });

  it('paints the canvas and text the theme renders for each scheme', () => {
    const [light, dark] = css.split(':root[data-mui-color-scheme="dark"]');
    for (const [block, scheme] of [[light, 'light'], [dark, 'dark']] as const) {
      const palette = theme.colorSchemes[scheme]?.palette;
      expect(block).toContain('color-scheme: ' + scheme + ';');
      expect(block).toContain('background-color: ' + palette?.background.default + ';');
      expect(block).toContain('color: ' + palette?.text.primary + ';');
    }
    expect(theme.getColorSchemeSelector('dark')).toContain('[data-mui-color-scheme="dark"]');
  });
});

describe('first-paint fonts', () => {
  const indexCss = fs.readFileSync(path.resolve(process.cwd(), 'src/index.css'), 'utf8');

  it('defines a fallback face for every fallback family in the font stacks', () => {
    const families = [brandTypography.fontFamily, brandTypography.displayNumeric]
      .flatMap((stack) => stack.match(/"[^"]+ Fallback"/g) ?? [])
      .map((quoted) => quoted.slice(1, -1));
    expect(families).toEqual(['Inter Variable Fallback', 'Space Grotesk Variable Fallback']);
    for (const family of families) {
      expect(indexCss).toContain(`font-family: '${family}';`);
    }
  });

  it('names the installed font versions the fallback metrics were measured against', () => {
    const measured = /Measured against (\S+) and (\S+);/.exec(indexCss)?.slice(1);
    expect(measured).toHaveLength(2);
    for (const pkg of ['@fontsource-variable/inter', '@fontsource-variable/space-grotesk']) {
      const manifest = fs.readFileSync(path.resolve(process.cwd(), 'node_modules', pkg, 'package.json'), 'utf8');
      const { version } = JSON.parse(manifest) as { version: string };
      expect(measured).toContain(`${pkg}@${version}`);
    }
  });

  it('preloads exactly the latin subset of each imported font', () => {
    const backend = fs.readFileSync(path.resolve(process.cwd(), '../app/api/endpoints/frontend.py'), 'utf8');
    const source = /_FIRST_PAINT_FONT = re\.compile\(r"([^"]+)"\)/.exec(backend)?.[1];
    expect(source).toBeDefined();
    const preloaded = new RegExp(source!);
    const imports = [...indexCss.matchAll(/@import '(@fontsource-variable\/[^']+\.css)';/g)].map((match) => match[1]);
    expect(imports).toHaveLength(2);
    for (const specifier of imports) {
      const css = fs.readFileSync(path.resolve(process.cwd(), 'node_modules', specifier), 'utf8');
      const faces = [...css.matchAll(/url\(\.\/files\/([\w-]+)\.woff2\)[\s\S]*?unicode-range: ([^;]+);/g)];
      expect(faces.length).toBeGreaterThan(1);
      // Vite emits bundled assets as `[name]-[hash][extname]`.
      const selected = faces.filter(([, name]) => preloaded.test(`assets/${name}-Hash_0-9.woff2`));
      expect(selected.map(([, , range]) => range.startsWith('U+0000-00FF,'))).toEqual([true]);
    }
  });
});