// Actual pinned Playwright callbacks, with synthetic tests only. No browser,
// Brain, sockets, account data, or product fixture is started by these probes.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { stripVTControlCharacters } from 'node:util';
import { E2E_ROOT, readRegistry } from './registry.mjs';
import { childEnvironment, progressLine } from './run.mjs';
import { verifyEvents } from './verify.mjs';

const ACTUAL = 'browser_sentinel_actual_73619';
const EXPECTED = 'browser_sentinel_expected_92641';
const RAW = 'browser_sentinel_raw_18527';
const TEST_ID = 'synthetic-case';
const reporterPath = path.join(E2E_ROOT, 'ci', 'reporter.mjs');
const cli = path.join(E2E_ROOT, 'node_modules', '@playwright', 'test', 'cli.js');
const cases = [
  'pass', 'retry', 'fail', 'skip', 'fixme', 'expected-failure', 'unexpected-pass',
  'timeout', 'before-all', 'global-setup', 'unknown-identity', 'duplicate-identity', 'missing-terminal',
  'constructor-failure', 'raw-boundary', 'redacted-assertion', 'interrupted',
  'worker-crash', 'reporter-test-end-throw', 'reporter-end-throw',
];
const FAILURE_CODES = ['token-evidence', 'token-stdout', 'token-stderr', 'success-exit',
  'missing-redaction', 'missing-frame', 'failure-exit', 'missing-interruption', 'probe-assertion',
  'assertion-boundary-inactive', 'assertion-boundary-not-cleared', 'assertion-boundary-not-restored',
  'redactor-message-token', 'redactor-stack-token', 'redactor-cause-token', 'redactor-errors-token',
  'reported-step-token', 'reported-result-token', 'token-console-source-excerpt',
  'token-console-expected', 'token-console-received', 'token-console-cause',
  'token-console-json', 'token-console-unknown', 'reported-global-token',
  'reported-stdout-token', 'reported-stderr-token', 'missing-observer-callback'];
function check(condition, code) { if (!condition) throw new Error(code); }

function classifyConsoleToken(output) {
  const plain = stripVTControlCharacters(output).split(/\r?\n/)
    .find(value => [ACTUAL, EXPECTED, RAW].some(token => value.includes(token)));
  if (!plain) return null;
  if (/^\s*(?:>\s*)?\d+\s*\|/.test(plain)) return 'token-console-source-excerpt';
  if (/Expected:/.test(plain)) return 'token-console-expected';
  if (/Received:/.test(plain)) return 'token-console-received';
  if (/(?:cause|Caused by):/.test(plain)) return 'token-console-cause';
  if (/^\s*[\[{]/.test(plain)) return 'token-console-json';
  return 'token-console-unknown';
}

async function child(args, env, { cwd, interruptFile } = {}) {
  return new Promise((resolve, reject) => {
    const processChild = spawn(process.execPath, args, { cwd, env, windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = ''; let stderr = ''; let interrupted = false;
    processChild.stdout.on('data', (bytes) => { stdout += bytes; });
    processChild.stderr.on('data', (bytes) => { stderr += bytes; });
    const timer = setTimeout(() => { processChild.kill('SIGTERM'); reject(new Error('browser_ci_sentinel_timeout')); }, 30_000);
    const poll = interruptFile ? setInterval(async () => {
      if (interrupted) return;
      try {
        if ((await readFile(interruptFile, 'utf8')).includes('"event":"attempt_begin"')) {
          interrupted = true; processChild.kill('SIGINT');
        }
      } catch { /* The reporter has not opened its owned output yet. */ }
    }, 25) : null;
    const cleanup = () => { clearTimeout(timer); if (poll) clearInterval(poll); };
    processChild.once('error', () => { cleanup(); reject(new Error('browser_ci_sentinel_spawn_failed')); });
    processChild.once('close', (code, signal) => { cleanup(); resolve({ code, signal, stdout, stderr, interrupted }); });
  });
}

function body(scenario) {
  if (scenario === 'retry') return `if (info.retry === 0) throw new Error('synthetic first attempt');`;
  if (scenario === 'fail') return `throw new Error('synthetic expected failure');`;
  if (scenario === 'skip') return `test.skip(true, 'synthetic skip');`;
  if (scenario === 'fixme') return `test.fixme(true, 'synthetic fixme');`;
  if (scenario === 'expected-failure') return `test.fail(); throw new Error('synthetic expected failure');`;
  if (scenario === 'unexpected-pass') return `test.fail();`;
  if (scenario === 'timeout') return `test.setTimeout(50); await new Promise(() => {});`;
  if (scenario === 'interrupted') return `await new Promise((resolve) => setTimeout(resolve, 1000));`;
  if (scenario === 'worker-crash') return `process.exit(17);`;
  if (scenario === 'raw-boundary') return `
    console.log(process.env.BROWSER_SENTINEL_RAW);
    console.error(process.env.BROWSER_SENTINEL_RAW);
    info.annotations.push({ type: 'synthetic', description: process.env.BROWSER_SENTINEL_RAW });
    await info.attach(process.env.BROWSER_SENTINEL_RAW, { body: Buffer.from(process.env.BROWSER_SENTINEL_RAW), contentType: 'text/plain' });
    throw new Error(process.env.BROWSER_SENTINEL_RAW);
  `;
  if (scenario === 'redacted-assertion') return `
    const actual = process.env.BROWSER_SENTINEL_ACTUAL;
    const expected = process.env.BROWSER_SENTINEL_EXPECTED;
    const before = playwrightExpect.expectConfig();
    if (!before.testInfo) throw new Error('assertion-boundary-inactive');
    try {
      withoutReportedExpectStep(() => {
        if (playwrightExpect.expectConfig().testInfo !== null) throw new Error('assertion-boundary-not-cleared');
        return expect(actual).toBe(expected);
      }, playwrightExpect);
    } catch (error) {
      if (playwrightExpect.expectConfig() !== before) throw new Error('assertion-boundary-not-restored');
      error.cause = new Error(actual);
      error.errors = [new Error(expected)];
      const original = error;
      const redacted = redactStableConnectionAssertionError(error, actual, expected);
      if (redacted !== original) throw new Error('synthetic assertion replaced');
      const dirty = (value) => [actual, expected].some(token => stripVTControlCharacters(String(value)).includes(token));
      if (dirty(redacted.message)) throw new Error('redactor-message-token');
      if (dirty(redacted.stack)) throw new Error('redactor-stack-token');
      if (dirty(redacted.cause.message) || dirty(redacted.cause.stack)) throw new Error('redactor-cause-token');
      if (redacted.errors.some(value => dirty(value.message) || dirty(value.stack))) throw new Error('redactor-errors-token');
      throw redacted;
    }
  `;
  return `expect(1).toBe(1);`;
}

async function probe(root, scenario) {
  const directory = path.join(root, scenario); await mkdir(path.join(directory, 'tests'), { recursive: true });
  const registry = { schema: 'browser-ci-registry/v1', playwright_version: '1.63.0', project: '', repeat_index: 0,
    tests: [{ id: TEST_ID, file: 'tests/probe.spec.mjs', title_path: ['synthetic closed case'], shard: 'core', retry_override: null }] };
  const registryPath = path.join(directory, 'registry.json'); await writeFile(registryPath, JSON.stringify(registry));
  const { digest } = readRegistry(registryPath);
  const eventsPath = path.join(directory, 'execution.ndjson');
  const expectModule = pathToFileURL(path.join(E2E_ROOT, 'node_modules/playwright/lib/matchers/expect.js')).href;
  const redactionModule = pathToFileURL(path.join(E2E_ROOT, 'lib/stable-connection-diagnostic.mjs')).href;
  const title = scenario === 'unknown-identity' ? 'synthetic unknown case' : 'synthetic closed case';
  const source = `import { expect, test } from '@playwright/test';
import { stripVTControlCharacters } from 'node:util';
import playwrightExpect from ${JSON.stringify(expectModule)};
import { withoutReportedExpectStep, redactStableConnectionAssertionError } from ${JSON.stringify(redactionModule)};
${scenario === 'before-all' ? `test.beforeAll(() => { throw new Error('synthetic setup failure'); });` : ''}
test(${JSON.stringify(title)}, async ({}, info) => { ${body(scenario)} });
${scenario === 'duplicate-identity' ? `test(${JSON.stringify(title)}, async () => {});` : ''}
`;
  await writeFile(path.join(directory, 'tests/probe.spec.mjs'), source);
  // Explicit sink prevents Playwright's fallback console reporter only for the
  // raw-boundary probe. Production console reporters are never changed.
  const sinkPath = path.join(directory, 'sink.mjs');
  await writeFile(sinkPath, 'export default class { printsToStdio() { return true; } }\n');
  let selectedReporter = reporterPath;
  if (['missing-terminal', 'reporter-test-end-throw', 'reporter-end-throw'].includes(scenario)) {
    selectedReporter = path.join(directory, 'partial-reporter.mjs');
    const callback = scenario === 'reporter-test-end-throw' ? 'onTestEnd' : 'onEnd';
    const statement = scenario === 'missing-terminal' ? '' : "throw new Error('synthetic reporter callback failure');";
    await writeFile(selectedReporter, `import Reporter from ${JSON.stringify(pathToFileURL(reporterPath).href)};
export default class extends Reporter { ${callback}() { ${statement} } }\n`);
  }
  if (scenario === 'constructor-failure') await writeFile(eventsPath, '');
  const consoleReporter = scenario === 'redacted-assertion' ? ['line'] : [sinkPath];
  const config = {
    testDir: './tests', fullyParallel: false, workers: 1, retries: scenario === 'retry' ? 1 : 0,
    timeout: 180_000, outputDir: './test-results',
    reporter: [consoleReporter, [selectedReporter, { mode: 'execution', lane: 'core', root: directory, registryPath, file: eventsPath }]],
    use: { screenshot: 'off', trace: 'off', video: 'off' },
  };
  const boundaryFlags = path.join(directory, 'boundary-flags.txt');
  if (scenario === 'redacted-assertion') {
    const observer = path.join(directory, 'boundary-observer.mjs');
    await writeFile(observer, `import { appendFileSync } from 'node:fs';
import { stripVTControlCharacters } from 'node:util';
const tokens = [process.env.BROWSER_SENTINEL_ACTUAL, process.env.BROWSER_SENTINEL_EXPECTED];
const dirty = value => typeof value === 'string'
  ? tokens.some(token => stripVTControlCharacters(value).includes(token))
  : value && typeof value === 'object' && Object.values(value).some(dirty);
export default class {
  printsToStdio() { return false; }
  onError(error) {
    if (dirty(error)) appendFileSync(${JSON.stringify(boundaryFlags)}, 'reported-global-token\\n');
  }
  onStdOut(chunk) {
    if (dirty(String(chunk))) appendFileSync(${JSON.stringify(boundaryFlags)}, 'reported-stdout-token\\n');
  }
  onStdErr(chunk) {
    if (dirty(String(chunk))) appendFileSync(${JSON.stringify(boundaryFlags)}, 'reported-stderr-token\\n');
  }
  onStepEnd(test, result, step) {
    if (dirty(step.error)) appendFileSync(${JSON.stringify(boundaryFlags)}, 'reported-step-token\\n');
  }
  onTestEnd(test, result) {
    appendFileSync(${JSON.stringify(boundaryFlags)}, 'observed-test-end\\n');
    if (dirty(result.errors)) appendFileSync(${JSON.stringify(boundaryFlags)}, 'reported-result-token\\n');
  }
}\n`);
    config.reporter.unshift([observer]);
  }
  if (scenario === 'global-setup') {
    config.globalSetup = path.join(directory, 'global-setup.mjs');
    await writeFile(config.globalSetup, 'export default function () { throw new Error(process.env.BROWSER_SENTINEL_RAW); }\n');
  }
  await writeFile(path.join(directory, 'playwright.config.mjs'), `export default ${JSON.stringify(config)};\n`);
  const env = childEnvironment({ ...process.env, CI: 'true', BROWSER_SENTINEL_ACTUAL: ACTUAL,
    BROWSER_SENTINEL_EXPECTED: EXPECTED, BROWSER_SENTINEL_RAW: RAW }, 'execution', 'core', eventsPath);
  const output = await child([cli, 'test', '--config', path.join(directory, 'playwright.config.mjs')], env,
    { cwd: directory, interruptFile: scenario === 'interrupted' ? eventsPath : null });
  let bytes = '';
  try { bytes = await readFile(eventsPath, 'utf8'); } catch { /* Constructor can fail before opening evidence. */ }
  if (scenario === 'redacted-assertion') {
    // These fixed codes diagnose the boundary without exposing captured output.
    for (const code of FAILURE_CODES.filter(value => value.startsWith('assertion-boundary-') || value.startsWith('redactor-'))) {
      check(!(output.stdout + output.stderr).includes('Error: ' + code), code);
    }
    let flags = '';
    try { flags = await readFile(boundaryFlags, 'utf8'); } catch { /* No dirty callback was observed. */ }
    for (const code of ['reported-global-token', 'reported-stdout-token', 'reported-stderr-token']) {
      check(!flags.includes(code), code);
    }
    check(flags.includes('observed-test-end'), 'missing-observer-callback');
    check(!flags.includes('reported-step-token'), 'reported-step-token');
    check(!flags.includes('reported-result-token'), 'reported-result-token');
    const context = classifyConsoleToken(output.stdout + '\n' + output.stderr);
    if (context) throw new Error(context);
  }
  for (const token of [ACTUAL, EXPECTED, RAW]) {
    check(!bytes.includes(token), 'token-evidence');
    assert.ok(!progressLine('core', 'execution', 30, 1, 0).includes(token));
    check(!stripVTControlCharacters(output.stdout).includes(token), 'token-stdout');
    check(!stripVTControlCharacters(output.stderr).includes(token), 'token-stderr');
  }
  const options = { registry, digest, mode: 'execution', lane: 'core' };
  if (['pass', 'retry'].includes(scenario)) {
    check(output.code === 0, 'success-exit');
    const result = verifyEvents(bytes, options);
    assert.equal(result.completed, 1);
    if (scenario === 'retry') assert.throws(() => verifyEvents(bytes, { ...options, qualification: true }));
  } else {
    assert.throws(() => verifyEvents(bytes, options), undefined, 'incomplete or nonpassing execution was accepted');
    if (scenario === 'missing-terminal') assert.equal(output.code, 0, 'missing terminal probe must exercise a green child exit');
    if (scenario === 'redacted-assertion') {
      check(output.code !== 0, 'failure-exit');
      check(/\[redacted\]/.test(output.stdout + output.stderr), 'missing-redaction');
      check(/probe\.spec\.mjs/.test(output.stdout + output.stderr), 'missing-frame');
    }
    if (scenario === 'interrupted') check(output.interrupted, 'missing-interruption');
  }
  return { case: scenario, accepted: ['pass', 'retry'].includes(scenario), qualification_retry_rejected: scenario === 'retry' };
}

let activeCase = null;
async function main() {
  const pinned = JSON.parse(await readFile(path.join(E2E_ROOT, 'node_modules/@playwright/test/package.json'), 'utf8'));
  assert.equal(pinned.version, '1.63.0');
  const summaries = [];
  const requested = process.argv.slice(2);
  let selected = cases;
  if (requested.length) {
    if (requested.length !== 2 || requested[0] !== '--case' || !cases.includes(requested[1])) {
      throw new Error('browser_ci_invalid_sentinel_arguments');
    }
    selected = [requested[1]];
  }
  const root = await mkdtemp(path.join(E2E_ROOT, '.ci-sentinel-'));
  try {
    for (const scenario of selected) { activeCase = scenario; summaries.push(await probe(root, scenario)); }
    const summary = JSON.stringify({ schema: 'browser-ci-sentinels/v1', playwright_version: pinned.version,
      cases: summaries.length, passed: true });
    for (const token of [ACTUAL, EXPECTED, RAW]) assert.ok(!summary.includes(token));
    console.log(summary);
  } finally {
    // This is the fresh owned fixture directory, never an app profile or shared
    // test directory. No process enumeration or network cleanup is performed.
    if (path.dirname(root) !== E2E_ROOT || !path.basename(root).startsWith('.ci-sentinel-')) {
      throw new Error('browser_ci_sentinel_cleanup_boundary');
    }
    await rm(root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  const code = FAILURE_CODES.includes(error.message) ? error.message : 'probe-assertion';
  console.error(`browser-ci synthetic sentinel verification failed case=${cases.includes(activeCase) ? activeCase : 'setup'} code=${code}`);
  process.exitCode = 1;
});
