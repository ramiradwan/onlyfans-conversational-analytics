import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

// Historical pre-Pleasure-Pass evidence stays immutable. Protect unmodified contracts.
const baseline = JSON.parse(fs.readFileSync(path.resolve('tests/fixtures/theme-appearance-baseline.json'), 'utf8')) as {
  revision: string; exports: Record<string, string>; staticDeclarations: string[];
};

describe('Pleasure Pass nonvisual compatibility', () => {
  it('preserves original brand primitives, layout dimensions and shape', () => {
    const source = fs.readFileSync(path.resolve('src/theme/generated/tokens.ts'), 'utf8').replace(/\r\n/g, '\n');
    for (const name of ['brandPalette', 'layoutTokens', 'shape']) {
      const json = new RegExp('export const ' + name + ' = ([\\s\\S]*?) as const;').exec(source)?.[1];
      expect(json, name + ' at ' + baseline.revision).toBeDefined();
      expect(createHash('sha256').update(json!).digest('hex'), name).toBe(baseline.exports[name]);
    }
  });
  it('preserves static prose typography, dimensions and motion', () => {
    const css = fs.readFileSync(path.resolve('src/theme/generated/static-tokens.css'), 'utf8');
    const protectedDeclarations = baseline.staticDeclarations.filter((line) =>
      !/^--dipsy-(color|shadow|rim|glow)/.test(line));
    const oldNames = new Set(protectedDeclarations.map((line) => line.split(':')[0]));
    const declarations = (css.match(/--dipsy-[^;\n]+;/g) ?? []).filter((line) => oldNames.has(line.split(':')[0]));
    expect(declarations).toEqual(protectedDeclarations);
  });
});
