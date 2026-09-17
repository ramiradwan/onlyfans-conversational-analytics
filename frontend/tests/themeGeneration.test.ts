import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { generateThemeSource } from '../src/theme/generate-theme';

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
