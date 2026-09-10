import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { cruise } from 'dependency-cruiser';
import extractDepcruiseOptions from 'dependency-cruiser/config-utl/extract-depcruise-options';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const FRONTEND_ROOT = path.resolve(__dirname, '..');
const CONFIG_PATH = path.join(FRONTEND_ROOT, '.dependency-cruiser.cjs');
const FIXTURES_ROOT = path.join(FRONTEND_ROOT, 'test-fixtures', 'architecture-invalid');

const baseOptions = await extractDepcruiseOptions(CONFIG_PATH);

describe('Bridge architecture boundaries and acyclic kernel enforcement', () => {
  it('production Bridge protected kernel graph parses and passes all architecture boundary rules', async () => {
    const result = await cruise(['src/protocol', 'src/store', 'src/services'], {
      ...baseOptions,
      baseDir: FRONTEND_ROOT,
    });

    if (typeof result.output === 'string') {
      throw new Error(`Unexpected string output from cruise: ${result.output}`);
    }

    const summary = result.output.summary;
    expect(summary.error).toBe(0);
    expect(summary.violations).toHaveLength(0);
    expect(summary.totalCruised).toBeGreaterThan(0);
    expect(summary.totalDependenciesCruised).toBeGreaterThan(0);
  });

  it('rejects circular dependency passing through protected store and service modules with structured cycle diagnostic', async () => {
    const fixtureDir = path.join(FIXTURES_ROOT, 'protected-cycle');
    const result = await cruise(['services', 'store'], {
      ...baseOptions,
      baseDir: fixtureDir,
    });

    if (typeof result.output === 'string') {
      throw new Error(`Unexpected string output from cruise: ${result.output}`);
    }

    const summary = result.output.summary;
    expect(summary.totalCruised).toBeGreaterThan(0);
    expect(summary.violations.length).toBeGreaterThan(0);

    const cycleViolation = summary.violations.find(
      (v) => v.rule.name === 'rule-bridge-protected-acyclic'
    );
    expect(cycleViolation).toBeDefined();
    expect(cycleViolation?.rule.severity).toBe('error');
    expect(cycleViolation?.cycle).toBeDefined();
    expect(cycleViolation?.cycle?.length).toBeGreaterThanOrEqual(2);

    const cycleNames = cycleViolation?.cycle?.map((c) => c.name) ?? [];
    expect(cycleNames.some((n) => /store\//.test(n))).toBe(true);
    expect(cycleNames.some((n) => /services\//.test(n))).toBe(true);
  });

  it('rejects circular dependency passing through protected protocol modules with structured cycle diagnostic', async () => {
    const fixtureDir = path.join(FIXTURES_ROOT, 'protected-cycle-protocol');
    const result = await cruise(['protocol'], {
      ...baseOptions,
      baseDir: fixtureDir,
    });

    if (typeof result.output === 'string') {
      throw new Error(`Unexpected string output from cruise: ${result.output}`);
    }

    const summary = result.output.summary;
    expect(summary.totalCruised).toBeGreaterThan(0);
    expect(summary.violations.length).toBeGreaterThan(0);

    const cycleViolation = summary.violations.find(
      (v) => v.rule.name === 'rule-bridge-protected-acyclic'
    );
    expect(cycleViolation).toBeDefined();
    expect(cycleViolation?.rule.severity).toBe('error');
    expect(cycleViolation?.cycle).toBeDefined();
    expect(cycleViolation?.cycle?.length).toBeGreaterThanOrEqual(2);

    const cycleNames = cycleViolation?.cycle?.map((c) => c.name) ?? [];
    expect(cycleNames.some((n) => /protocol\//.test(n))).toBe(true);
  });

  it('positive control: does not reject circular dependencies outside the protected protocol/store/service kernel', async () => {
    const fixtureDir = path.join(FIXTURES_ROOT, 'circular-unprotected');
    const result = await cruise(['components'], {
      ...baseOptions,
      baseDir: fixtureDir,
    });

    if (typeof result.output === 'string') {
      throw new Error(`Unexpected string output from cruise: ${result.output}`);
    }

    const summary = result.output.summary;
    expect(summary.totalCruised).toBeGreaterThan(0);
    expect(summary.violations).toHaveLength(0);
  });

  it('proves fixture modules are excluded from normal Vitest discovery and TypeScript build inputs', () => {
    // Verify Vitest test include glob strictly matches tests/ and excludes test-fixtures/
    const vitestConfigText = fs.readFileSync(path.join(FRONTEND_ROOT, 'vitest.config.ts'), 'utf-8');
    expect(vitestConfigText).toContain("include: ['tests/**/*.test.{ts,tsx}']");
    expect(vitestConfigText).not.toContain('test-fixtures');

    // Verify tsconfig.json includes only src and tooling configs, not test-fixtures/
    const tsConfigText = fs.readFileSync(path.join(FRONTEND_ROOT, 'tsconfig.json'), 'utf-8');
    const tsConfig = JSON.parse(tsConfigText);
    expect(tsConfig.include).toBeDefined();
    for (const inc of tsConfig.include) {
      expect(inc).not.toMatch(/test-fixtures/);
    }
  });
});
