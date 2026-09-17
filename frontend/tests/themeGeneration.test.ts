import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  generateStaticTokensCss,
  generateThemeSource,
  replaceStaticTokenBlock,
  staticTokenConsumers,
} from '../src/theme/generate-theme';

describe('theme token generation', () => {
  it('is deterministic and matches the checked-in generated source', () => {
    const tokenPath = path.resolve(process.cwd(), 'src/theme/tokens.json');
    const generatedPath = path.resolve(process.cwd(), 'src/theme/generated/tokens.ts');
    const source = fs.readFileSync(tokenPath, 'utf8');
    const first = generateThemeSource(source);
    const second = generateThemeSource(source);
    const checkedIn = fs.readFileSync(generatedPath, 'utf8').replace(/\r\n/g, '\n');

    expect(first).toBe(second);
    expect(first).toBe(checkedIn);
  });

  it('leads the brand font stack with the family the bundled stylesheet declares', () => {
    const tokens = JSON.parse(
      fs.readFileSync(path.resolve(process.cwd(), 'src/theme/tokens.json'), 'utf8'),
    ) as { tier1: { brandTypography: { fontFamily: string } } };
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

  it.each(staticTokenConsumers)('%s carries the current generated token block', (consumer) => {
    const source = fs
      .readFileSync(path.resolve(process.cwd(), '..', consumer), 'utf8')
      .replace(/\r\n/g, '\n');
    expect(replaceStaticTokenBlock(source, css)).toBe(source);
  });

  it('detects a hand-edited token value inside the block', () => {
    const consumer = ['a {}', '  /* design-tokens:start */', '  /* design-tokens:end */', ''].join('\n');
    const synced = replaceStaticTokenBlock(consumer, css);
    const edited = synced.replace('--dipsy-color-primary: #5B57F2;', '--dipsy-color-primary: #126E73;');
    expect(edited).not.toBe(synced);
    expect(replaceStaticTokenBlock(edited, css)).toBe(synced);
  });

  it('refuses consumers without exactly one marker pair', () => {
    expect(() => replaceStaticTokenBlock('a {}', css)).toThrow(/marker pair/);
    const pair = '/* design-tokens:start */\n/* design-tokens:end */\n';
    expect(() => replaceStaticTokenBlock(pair + pair, css)).toThrow(/marker pair/);
  });

  it('derives on-primary text from the scheme contrast threshold', () => {
    const [light, dark] = css.split('@media (prefers-color-scheme: dark)');
    expect(light).toContain('--dipsy-color-on-primary: #FFFFFF;');
    expect(dark).toContain('--dipsy-color-on-primary: rgba(0, 0, 0, 0.87);');
  });
});
