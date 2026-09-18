import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const INTENTS = ['primary', 'secondary', 'accent', 'calm', 'success', 'warning', 'error', 'info'] as const;

type IntentTone = {
  main: string;
  light: string;
  dark: string;
};

type TokenSource = {
  tier2: {
    colorSchemes: Record<'light' | 'dark', Record<string, unknown>>;
  };
};

function sourceFiles(root: string): string[] {
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const resolved = path.join(root, entry.name);
    if (entry.isDirectory()) return sourceFiles(resolved);
    return entry.isFile() ? [resolved] : [];
  });
}

describe('native color theme contract', () => {
  it('authors every palette intent with explicit light, main and dark tones', () => {
    const tokens = JSON.parse(
      fs.readFileSync(path.resolve(process.cwd(), 'src/theme/tokens.json'), 'utf8'),
    ) as TokenSource;

    for (const schemeName of ['light', 'dark'] as const) {
      const scheme = tokens.tier2.colorSchemes[schemeName];
      for (const intent of INTENTS) {
        const tone = scheme[intent] as IntentTone;
        expect(tone.main).toBeTruthy();
        expect(tone.light).toBeTruthy();
        expect(tone.dark).toBeTruthy();
      }
    }
  });

  it('keeps MUI native color enabled without delegating core tonal roles to augmentColor', () => {
    const themeSource = fs.readFileSync(
      path.resolve(process.cwd(), 'src/theme/createTheme.ts'),
      'utf8',
    );

    expect(themeSource).toContain('nativeColor: true');
    expect(themeSource).not.toContain('augmentColor');
    expect(themeSource).not.toContain('nativeColorSeed');
  });

  it('keeps governed React UI colors on semantic tokens and OKLCH derivation', () => {
    const sourceRoot = path.resolve(process.cwd(), 'src');
    const ignored = new Set([
      path.resolve(sourceRoot, 'theme/createTheme.ts'),
      path.resolve(sourceRoot, 'theme/generate-theme.ts'),
    ]);
    const governed = sourceFiles(sourceRoot).filter((file) => {
      if (!/\.(css|ts|tsx)$/.test(file)) return false;
      if (ignored.has(file)) return false;
      return !file.includes(`${path.sep}theme${path.sep}generated${path.sep}`);
    });

    for (const file of governed) {
      const source = fs.readFileSync(file, 'utf8');
      expect(source, file).not.toMatch(/color-mix\(\s*in\s+srgb/i);
      expect(source, file).not.toMatch(/#[0-9a-f]{3,8}\b/i);
      expect(source, file).not.toMatch(/rgba?\(/i);
    }
  });
});
