import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

// Update only alongside an intentional, reviewed visual change, not regeneration.
const baseline = JSON.parse(fs.readFileSync(path.resolve('tests/fixtures/theme-appearance-baseline.json'), 'utf8')) as {
  revision: string; exports: Record<string, string>; staticDeclarations: string[];
};

describe('appearance-preserving intent migration', () => {
  it('preserves every legacy generated export from the pinned PR head', () => {
    const source = fs.readFileSync(path.resolve('src/theme/generated/tokens.ts'), 'utf8').replace(/\r\n/g, '\n');
    for (const [name, hash] of Object.entries(baseline.exports)) {
      const json = new RegExp('export const ' + name + ' = ([\\s\\S]*?) as const;').exec(source)?.[1];
      expect(json, name + ' at ' + baseline.revision).toBeDefined();
      expect(createHash('sha256').update(json!).digest('hex'), name).toBe(hash);
    }
  });
  it('preserves every existing static declaration in both schemes', () => {
    const css = fs.readFileSync(path.resolve('src/theme/generated/static-tokens.css'), 'utf8');
    const oldNames = new Set(baseline.staticDeclarations.map((line) => line.split(':')[0]));
    const declarations = (css.match(/--dipsy-[^;\n]+;/g) ?? []).filter((line) => oldNames.has(line.split(':')[0]));
    expect(declarations).toEqual(baseline.staticDeclarations);
  });
});
