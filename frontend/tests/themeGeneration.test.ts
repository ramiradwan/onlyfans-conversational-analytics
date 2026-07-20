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
});
