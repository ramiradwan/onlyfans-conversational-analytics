import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { cruise } from 'dependency-cruiser';
import extractDepcruiseOptions from 'dependency-cruiser/config-utl/extract-depcruise-options';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const EXTENSION_ROOT = path.resolve(__dirname, '..');
const CONFIG_PATH = path.join(EXTENSION_ROOT, '.dependency-cruiser.cjs');
const FIXTURES_ROOT = path.join(EXTENSION_ROOT, 'test-fixtures', 'architecture-invalid');

// Extract base options from the authoritative .dependency-cruiser.cjs
const baseOptions = await extractDepcruiseOptions(CONFIG_PATH);

test('production Agent dependency graph passes all architecture boundary rules', async () => {
  const result = await cruise(['capture', 'protocol', 'transport', 'runtime'], {
    ...baseOptions,
    baseDir: EXTENSION_ROOT,
  });

  assert.equal(
    result.output.summary.violations.length,
    0,
    `Expected 0 violations in production Agent graph, but found: ${JSON.stringify(result.output.summary.violations, null, 2)}`
  );
  assert.equal(result.output.summary.error, 0);
  assert.ok(result.output.summary.totalCruised > 0, 'Production Agent graph must contain modules');
  assert.ok(result.output.summary.totalDependenciesCruised > 0, 'Production Agent graph must contain dependencies');
});

test('Rule A negative control: capture importing a transport implementation is rejected', async () => {
  const fixtureDir = path.join(FIXTURES_ROOT, 'capture-imports-transport');
  const result = await cruise(['capture', 'transport'], {
    ...baseOptions,
    baseDir: fixtureDir,
  });

  // Ensure tool/config did not fail before graph inspection
  assert.ok(result.output.summary.totalCruised > 0, 'Dependency-cruiser failed to parse fixture graph');

  const violations = result.output.summary.violations;
  assert.equal(violations.length, 1, `Expected exactly 1 violation, got: ${JSON.stringify(violations)}`);

  const violation = violations[0];
  assert.equal(violation.rule.name, 'rule-agent-capture-isolation');
  assert.equal(violation.rule.severity, 'error');
  assert.match(violation.from, /(^|\/)capture\/envelopes\.mjs$/);
  assert.match(violation.to, /(^|\/)transport\/durable-outbox\.mjs$/);
});

test('Rule A negative control: capture importing consent-controller is rejected', async () => {
  const fixtureDir = path.join(FIXTURES_ROOT, 'capture-imports-consent-controller');
  const result = await cruise(['capture', 'runtime'], {
    ...baseOptions,
    baseDir: fixtureDir,
  });

  assert.ok(result.output.summary.totalCruised > 0, 'Dependency-cruiser failed to parse fixture graph');

  const violations = result.output.summary.violations;
  assert.equal(violations.length, 1, `Expected exactly 1 violation, got: ${JSON.stringify(violations)}`);

  const violation = violations[0];
  assert.equal(violation.rule.name, 'rule-agent-capture-isolation');
  assert.equal(violation.rule.severity, 'error');
  assert.match(violation.from, /(^|\/)capture\/envelopes\.mjs$/);
  assert.match(violation.to, /(^|\/)runtime\/consent-controller\.mjs$/);
});

test('Rule A negative control: capture importing legal-activation-controller is rejected', async () => {
  const fixtureDir = path.join(FIXTURES_ROOT, 'capture-imports-legal-activation-controller');
  const result = await cruise(['capture', 'runtime'], {
    ...baseOptions,
    baseDir: fixtureDir,
  });

  assert.ok(result.output.summary.totalCruised > 0, 'Dependency-cruiser failed to parse fixture graph');

  const violations = result.output.summary.violations;
  assert.equal(violations.length, 1, `Expected exactly 1 violation, got: ${JSON.stringify(violations)}`);

  const violation = violations[0];
  assert.equal(violation.rule.name, 'rule-agent-capture-isolation');
  assert.equal(violation.rule.severity, 'error');
  assert.match(violation.from, /(^|\/)capture\/envelopes\.mjs$/);
  assert.match(violation.to, /(^|\/)runtime\/legal-activation-controller\.mjs$/);
});

test('Rule A negative control: capture importing legal-consent-authorization is rejected', async () => {
  const fixtureDir = path.join(FIXTURES_ROOT, 'capture-imports-legal-consent-authorization');
  const result = await cruise(['capture', 'runtime'], {
    ...baseOptions,
    baseDir: fixtureDir,
  });

  assert.ok(result.output.summary.totalCruised > 0, 'Dependency-cruiser failed to parse fixture graph');

  const violations = result.output.summary.violations;
  assert.equal(violations.length, 1, `Expected exactly 1 violation, got: ${JSON.stringify(violations)}`);

  const violation = violations[0];
  assert.equal(violation.rule.name, 'rule-agent-capture-isolation');
  assert.equal(violation.rule.severity, 'error');
  assert.match(violation.from, /(^|\/)capture\/envelopes\.mjs$/);
  assert.match(violation.to, /(^|\/)runtime\/legal-consent-authorization\.mjs$/);
});

test('Rule B negative control: circular dependency passing through transport and runtime is rejected', async () => {
  const fixtureDir = path.join(FIXTURES_ROOT, 'circular-transport-runtime');
  const result = await cruise(['transport', 'runtime'], {
    ...baseOptions,
    baseDir: fixtureDir,
  });

  assert.ok(result.output.summary.totalCruised > 0, 'Dependency-cruiser failed to parse fixture graph');

  const violations = result.output.summary.violations;
  assert.ok(violations.length > 0, 'Expected at least 1 cycle violation');

  const cycleViolation = violations.find((v) => v.rule.name === 'rule-agent-protected-acyclic');
  assert.ok(cycleViolation, `Expected rule-agent-protected-acyclic violation, found: ${JSON.stringify(violations)}`);
  assert.equal(cycleViolation.rule.severity, 'error');
  assert.ok(
    cycleViolation.cycle && cycleViolation.cycle.length >= 2,
    `Expected cycle diagnostic array, got: ${JSON.stringify(cycleViolation.cycle)}`
  );
  assert.ok(
    cycleViolation.cycle.some((c) => /transport\//.test(c.name))
    && cycleViolation.cycle.some((c) => /runtime\//.test(c.name)),
    'Cycle diagnostic must identify protected transport and runtime modules'
  );
});

test('Rule B negative control: circular dependency passing through protocol is rejected', async () => {
  const fixtureDir = path.join(FIXTURES_ROOT, 'circular-protocol');
  const result = await cruise(['protocol'], {
    ...baseOptions,
    baseDir: fixtureDir,
  });

  assert.ok(result.output.summary.totalCruised > 0, 'Dependency-cruiser failed to parse fixture graph');

  const violations = result.output.summary.violations;
  assert.ok(violations.length > 0, 'Expected at least 1 cycle violation');

  const cycleViolation = violations.find((v) => v.rule.name === 'rule-agent-protected-acyclic');
  assert.ok(cycleViolation, `Expected rule-agent-protected-acyclic violation, found: ${JSON.stringify(violations)}`);
  assert.equal(cycleViolation.rule.severity, 'error');
  assert.ok(
    cycleViolation.cycle && cycleViolation.cycle.length >= 2,
    `Expected cycle diagnostic array, got: ${JSON.stringify(cycleViolation.cycle)}`
  );
  assert.ok(
    cycleViolation.cycle.some((c) => /protocol\//.test(c.name)),
    'Cycle diagnostic must identify protected protocol module'
  );
});

test('Rule B positive control: circular dependencies outside protected kernel are not rejected', async () => {
  const fixtureDir = path.join(FIXTURES_ROOT, 'circular-unprotected-preview');
  const result = await cruise(['preview'], {
    ...baseOptions,
    baseDir: fixtureDir,
  });

  assert.ok(result.output.summary.totalCruised > 0, 'Dependency-cruiser failed to parse fixture graph');
  assert.equal(
    result.output.summary.violations.length,
    0,
    'Cycles outside the protected kernel must not trigger its acyclicity rule'
  );
});
