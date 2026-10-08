import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import RestrictedReporter from '../reporter.mjs';
import { readRegistry, identifyTest, laneTests } from '../registry.mjs';
import { verifyEvents } from '../verify.mjs';
import { parseOptions, childEnvironment, checkedOutSource, progressLine, startProgress } from '../run.mjs';

function fixture(t, { mode = 'execution', retries = 1 } = {}) {
  const root = mkdtempSync(path.join(tmpdir(), 'browser-ci-unit-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const registry = { schema: 'browser-ci-registry/v1', playwright_version: '1.63.0', project: '', repeat_index: 0,
    tests: [{ id: 'closed-case', file: 'tests/probe.spec.mjs', title_path: ['private-title'], shard: 'core', retry_override: null }] };
  const registryPath = path.join(root, 'registry.json'); writeFileSync(registryPath, JSON.stringify(registry));
  const { digest } = readRegistry(registryPath);
  const filename = path.join(root, 'events.ndjson');
  const reporter = new RestrictedReporter({ mode, lane: 'core', root, registryPath, file: filename });
  const item = { title: 'private-title', location: { file: path.join(root, 'tests/probe.spec.mjs') },
    parent: { type: 'file', project: () => ({ name: '' }) }, repeatEachIndex: 0, retries, expectedStatus: 'passed',
    outcome: () => 'expected', annotations: [{ description: 'private-annotation' }] };
  reporter.onBegin({ workers: 1, projects: [{ retries, timeout: 180_000 }], version: '1.63.0' }, { allTests: () => [item] });
  return { reporter, item, filename, root, registry, digest, mode, lane: 'core' };
}

function attempt(f, retry = 0, status = 'passed') {
  const result = { retry, status, workerIndex: retry, duration: 10,
    stdout: ['private-stdout'], stderr: ['private-stderr'], errors: [{ message: 'private-error' }],
    attachments: [{ name: 'private-attachment', body: Buffer.from('private-body') }] };
  f.reporter.onTestBegin(f.item, result); f.reporter.onTestEnd(f.item, result);
}

test('closed reporter excludes titles, diagnostics, raw results and attachments', (t) => {
  const f = fixture(t); attempt(f); f.reporter.onEnd({ status: 'passed', error: 'private-terminal' });
  const bytes = readFileSync(f.filename, 'utf8');
  assert.doesNotMatch(bytes, /private-/);
  assert.deepEqual(verifyEvents(bytes, f), { count: 1, completed: 1, retried: 0 });
  assert.equal(f.reporter.printsToStdio(), false);
});

test('actual inventory includes all registry cases even for a selected lane and has no execution claim', (t) => {
  const f = fixture(t, { mode: 'inventory' }); f.reporter.onEnd({ status: 'passed' });
  assert.equal(f.reporter.printsToStdio(), true, 'inventory must not acquire a raw-title fallback console reporter');
  assert.deepEqual(verifyEvents(readFileSync(f.filename, 'utf8'), f), { count: 1, completed: 0, retried: 0 });
  const real = readRegistry().registry;
  const core = laneTests(real, 'core'); const catchup = laneTests(real, 'catchup');
  assert.equal(core.length, 13); assert.equal(catchup.length, 2);
  assert.equal(new Set([...core, ...catchup].map((item) => item.id)).size, real.tests.length);
  assert.deepEqual(laneTests(real, 'catchup', 'inventory'), real.tests);
  assert.equal(new Set(catchup.map((item) => item.file)).size, 1);
});

test('ordinary retry success is explicit and qualification refuses the same execution', (t) => {
  const f = fixture(t); attempt(f, 0, 'failed'); attempt(f, 1, 'passed');
  f.item.outcome = () => 'flaky'; f.reporter.onEnd({ status: 'passed' });
  const bytes = readFileSync(f.filename, 'utf8');
  assert.equal(verifyEvents(bytes, f).retried, 1);
  assert.throws(() => verifyEvents(bytes, { ...f, qualification: true }), /browser_ci_invalid_evidence/);
});

test('unknown identities and reporter initialization failures expose only fixed codes', (t) => {
  const f = fixture(t); f.item.title = 'private-new-identity';
  assert.equal(identifyTest(f.item, f.registry, f.root), null);
  const unknownPath = path.join(f.root, 'unknown.ndjson');
  const reporter = new RestrictedReporter({ mode: 'execution', lane: 'core', root: f.root,
    registryPath: path.join(f.root, 'registry.json'), file: unknownPath });
  reporter.onBegin({ workers: 1, projects: [{ retries: 1, timeout: 180_000 }], version: '1.63.0' }, { allTests: () => [f.item] });
  reporter.onError(new Error('private-error')); reporter.onEnd({ status: 'failed' });
  const bytes = readFileSync(unknownPath, 'utf8'); assert.doesNotMatch(bytes, /private-/);
  assert.throws(() => verifyEvents(bytes, f));
  assert.throws(() => new RestrictedReporter({ mode: 'execution', lane: 'core', file: path.join(f.root, 'private-missing/out'),
    registryPath: path.join(f.root, 'registry.json') }), { message: 'browser_ci_reporter_initialization_failed' });
});

test('malformed or incomplete event streams cannot turn a green child exit into success', (t) => {
  const f = fixture(t); attempt(f); f.reporter.onEnd({ status: 'passed' });
  const original = readFileSync(f.filename, 'utf8').trimEnd().split('\n').map(JSON.parse);
  const encode = (events) => events.map((event, seq) => JSON.stringify({ ...event, seq })).join('\n') + '\n';
  const cases = [
    original.slice(0, -1), [...original, original.at(-1)],
    original.filter((event) => event.event !== 'attempt_end'),
    original.filter((event) => event.event !== 'outcome'),
    original.map((event) => event.event === 'attempt_begin' ? { ...event, retry: 1 } : event),
    original.map((event) => event.event === 'attempt_end' ? { ...event, status: 'skipped' } : event),
    original.map((event) => event.event === 'attempt_end' ? { ...event, expected_status: 'failed' } : event),
    original.map((event) => event.event === 'outcome' ? { ...event, outcome: 'flaky' } : event),
    original.map((event) => event.event === 'end' ? { ...event, status: 'interrupted' } : event),
    original.map((event) => event.event === 'collection' ? { ...event, tests: [...event.tests, ...event.tests] } : event),
    original.map((event) => ({ ...event, raw_error: 'private-extra' })),
  ];
  for (const events of cases) assert.throws(() => verifyEvents(encode(events), f), /browser_ci_invalid_evidence/);
  assert.throws(() => verifyEvents(encode(original).replace('"seq":0', '"seq":0,"seq":0'), f));
});

test('runner selection rejects arbitrary pytest-style filters and clears Playwright overrides', () => {
  assert.equal(parseOptions(['--lane', 'all', '--output-dir', 'out'], {}).lane, 'legacy');
  assert.equal(parseOptions(['--lane', 'core', '--output-dir', 'out'], { BROWSER_QUALIFICATION: 'true' }).qualification, true);
  assert.throws(() => parseOptions(['--lane', 'core', '--output-dir', 'out', '--grep', 'private-title'], {}));
  const env = childEnvironment({ CI: 'true', PW_TEST_REPORTER: 'private-path', PW_TEST_SOURCE_TRANSFORM: 'private-hook',
    PWDEBUG: '1', NODE_OPTIONS: '--require=private-path', PLAYWRIGHT_JSON_OUTPUT_FILE: 'private-path',
    PLAYWRIGHT_BROWSERS_PATH: 'cache', OFCA_E2E_PYTHON: 'python' }, 'inventory', 'core', 'inventory.ndjson');
  assert.equal(env.PLAYWRIGHT_BROWSERS_PATH, 'cache'); assert.equal(env.OFCA_E2E_PYTHON, 'python');
  assert.equal(env.CI, 'true'); assert.doesNotMatch(JSON.stringify(env), /private-|PWDEBUG|NODE_OPTIONS/);
  assert.equal(progressLine('core', 'execution', 30, 13, 1),
    'browser-ci progress lane=core phase=execution elapsed_seconds=30 collected=13 completed=1');
  assert.throws(() => progressLine('private-title', 'execution', 0, 0, 0));
});

test('registry rejects splitting a file across lanes', (t) => {
  const f = fixture(t); const altered = structuredClone(f.registry);
  altered.tests.push({ ...altered.tests[0], id: 'another-case', title_path: ['another'], shard: 'catchup' });
  const filename = path.join(f.root, 'split.json'); writeFileSync(filename, JSON.stringify(altered));
  assert.throws(() => readRegistry(filename), /browser_ci_duplicate_or_split_identity/);
});

test('source proof always resolves the actual checkout and refuses an environment mismatch', () => {
  const head = 'a'.repeat(40); let reads = 0;
  const readHead = () => { reads += 1; return `${head}\n`; };
  assert.equal(checkedOutSource({}, readHead), head);
  assert.equal(checkedOutSource({ PRODUCT_SHA: head }, readHead), head);
  assert.throws(() => checkedOutSource({ PRODUCT_SHA: 'b'.repeat(40) }, readHead),
    { message: 'browser_ci_source_mismatch' });
  assert.equal(reads, 3);
  assert.throws(() => checkedOutSource({ PRODUCT_SHA: head }, () => { throw new Error('private-git-error'); }),
    { message: 'browser_ci_source_unavailable' });
});

test('runner heartbeat advances every thirty seconds without waiting for test callbacks', (t) => {
  const f = fixture(t); const output = []; let now = 0; let callback; let cancelled = false;
  const stop = startProgress({ lane: 'core', phase: 'execution', filename: f.filename,
    expected: f.registry.tests, now: () => now, log: (line) => output.push(line),
    schedule: (fn, milliseconds) => { assert.equal(milliseconds, 30_000); callback = fn; return 'owned-timer'; },
    cancel: (timer) => { assert.equal(timer, 'owned-timer'); cancelled = true; } });
  now = 30_000; callback(); now = 60_000; callback();
  assert.equal(output.length, 2); assert.match(output[0], /elapsed_seconds=30 collected=1 completed=0$/);
  assert.match(output[1], /elapsed_seconds=60 collected=1 completed=0$/);
  assert.doesNotMatch(output.join('\n'), /private-/);
  stop(); assert.equal(cancelled, true);
});
