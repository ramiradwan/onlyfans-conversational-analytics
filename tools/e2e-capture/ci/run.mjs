import { spawn, execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { performance } from 'node:perf_hooks';
import { E2E_ROOT, LANES, laneTests, readRegistry, sha256 } from './registry.mjs';
import { verifyEvents } from './verify.mjs';

export function parseOptions(argv, env = process.env) {
  const result = { qualification: env.BROWSER_QUALIFICATION === 'true', collectOnly: false };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === '--qualification') result.qualification = true;
    else if (value === '--list') result.collectOnly = true;
    else if (['--lane', '--output-dir'].includes(value) && argv[index + 1] && !argv[index + 1].startsWith('--')) {
      const key = value === '--lane' ? 'lane' : 'outputDir';
      if (result[key]) throw new Error('browser_ci_invalid_arguments');
      result[key] = argv[++index];
    } else throw new Error('browser_ci_invalid_arguments');
  }
  if (result.lane === 'all') result.lane = 'legacy';
  if (!LANES.includes(result.lane) || !result.outputDir) throw new Error('browser_ci_invalid_arguments');
  result.outputDir = path.resolve(result.outputDir);
  return result;
}

export function progressLine(lane, phase, elapsed, count, completed) {
  if (!LANES.includes(lane) || !['inventory', 'execution'].includes(phase)
      || ![elapsed, count, completed].every((value) => Number.isSafeInteger(value) && value >= 0)) {
    throw new Error('browser_ci_invalid_progress');
  }
  return `browser-ci progress lane=${lane} phase=${phase} elapsed_seconds=${elapsed} collected=${count} completed=${completed}`;
}

export function childEnvironment(env, phase, lane, filename) {
  const result = { ...env };
  // Developer/debug/reporting overrides must not change the independent list or
  // create an additional raw reporter. Keep browser cache and harness settings.
  for (const key of Object.keys(result)) {
    if (/^(PW_TEST_|PWTEST_|PWDEBUG|PW_RUNNER_DEBUG|PLAYWRIGHT_(JSON|JUNIT|HTML|BLOB)_)/.test(key)
        || ['NODE_OPTIONS', 'PLAYWRIGHT_LAST_RUN_OUTPUT_FILE'].includes(key)) delete result[key];
  }
  return { ...result, BROWSER_CI_PHASE: phase, BROWSER_CI_LANE: lane, BROWSER_CI_EVENTS: filename };
}

export function checkedOutSource(env, readHead = () => execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd: E2E_ROOT, encoding: 'utf8', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
})) {
  // PRODUCT_SHA is the expected revision, never an alternative source of proof.
  // Resolve the checkout even when CI supplies an otherwise valid expected SHA.
  let source;
  try { source = readHead().trim(); }
  catch { throw new Error('browser_ci_source_unavailable'); }
  if (!/^[a-f0-9]{40}$/.test(source)
      || (env.PRODUCT_SHA !== undefined && env.PRODUCT_SHA !== source)) {
    throw new Error('browser_ci_source_mismatch');
  }
  return source;
}

function readProgress(filename, expected) {
  try {
    const bytes = readFileSync(filename, 'utf8');
    if (bytes.length > 1024 * 1024) return { count: 0, completed: 0 };
    const events = bytes.split('\n').filter(Boolean).map((line) => JSON.parse(line));
    const collection = events.find((event) => event.event === 'collection');
    const ids = new Set(expected.map((test) => test.id));
    const count = Array.isArray(collection?.tests)
      ? new Set(collection.tests.filter((test) => ids.has(test.id)).map((test) => test.id)).size : 0;
    const completed = new Set(events.filter((event) => event.event === 'attempt_end'
      && event.status === 'passed' && ids.has(event.id)).map((event) => event.id)).size;
    return { count, completed };
  } catch { return { count: 0, completed: 0 }; }
}

export function startProgress({ lane, phase, filename, expected, log = console.log,
  now = () => performance.now(), schedule = setInterval, cancel = clearInterval }) {
  const started = now();
  const timer = schedule(() => {
    const counts = readProgress(filename, expected);
    log(progressLine(lane, phase, Math.floor((now() - started) / 1000), counts.count, counts.completed));
  }, 30_000);
  return () => cancel(timer);
}

export async function runChild(args, env, { lane, phase, filename, expected, log = console.log }) {
  let interrupted = false; let signal = null;
  const child = spawn(process.execPath, args, { cwd: E2E_ROOT, env, stdio: 'inherit', windowsHide: true });
  const interrupt = (name) => {
    interrupted = true; signal = name;
    // Signal only this owned foreground child. Never enumerate/kill listeners,
    // browser profiles, Brain processes, or processes from another task.
    child.kill(name);
  };
  const onInt = () => interrupt('SIGINT'); const onTerm = () => interrupt('SIGTERM');
  process.on('SIGINT', onInt); process.on('SIGTERM', onTerm);
  const stopProgress = startProgress({ lane, phase, filename, expected, log });
  try {
    return await new Promise((resolve) => {
      child.once('error', () => resolve({ code: 1, interrupted, signal }));
      child.once('close', (code, childSignal) => resolve({ code: code ?? 1,
        interrupted: interrupted || Boolean(childSignal), signal: signal ?? childSignal }));
    });
  } finally {
    stopProgress(); process.off('SIGINT', onInt); process.off('SIGTERM', onTerm);
  }
}

export async function main(argv = process.argv.slice(2), env = process.env) {
  let options;
  try { options = parseOptions(argv, env); }
  catch { console.error('browser-ci: use --lane core|catchup|legacy --output-dir DIRECTORY [--qualification] [--list]'); return 2; }
  const startedAt = new Date().toISOString(); const started = performance.now();
  let source;
  try {
    source = checkedOutSource(env);
  } catch { console.error('browser-ci: source revision unavailable or mismatched'); return 2; }
  if (!/^[a-f0-9]{40}$/.test(source) || (env.GITHUB_RUN_ID && !/^\d+$/.test(env.GITHUB_RUN_ID))
      || (env.GITHUB_RUN_ATTEMPT && !/^[1-9]\d*$/.test(env.GITHUB_RUN_ATTEMPT))
      || !Number.isSafeInteger(Number(env.GITHUB_RUN_ATTEMPT ?? 1))) {
    console.error('browser-ci: invalid source or run identity'); return 2;
  }
  let registry; let digest;
  try { ({ registry, digest } = readRegistry()); }
  catch { console.error('browser-ci: invalid checked-in registry'); return 2; }
  const cli = path.join(E2E_ROOT, 'node_modules', '@playwright', 'test', 'cli.js');
  if (!existsSync(cli)) { console.error('browser-ci: run npm ci --prefix tools/e2e-capture'); return 2; }
  const inventoryPath = path.join(options.outputDir, 'inventory.ndjson');
  const executionPath = path.join(options.outputDir, 'execution.ndjson');
  const receiptPath = path.join(options.outputDir, 'receipt.json');
  const platform = ({ win32: 'Windows', linux: 'Linux', darwin: 'Darwin' })[process.platform];
  if (!platform) { console.error('browser-ci: unsupported runtime platform'); return 2; }
  if ([inventoryPath, executionPath, receiptPath].some(existsSync)) {
    console.error('browser-ci: evidence directory already contains a run; choose a fresh output directory'); return 2;
  }
  mkdirSync(options.outputDir, { recursive: true });
  const receipt = {
    schema: 'browser-ci-receipt/v1', source_commit: source, workflow_run_id: env.GITHUB_RUN_ID ?? 'local',
    run_attempt: Number(env.GITHUB_RUN_ATTEMPT ?? 1),
    logical_job: options.lane === 'legacy' ? 'browser-e2e-serial-control' : `browser-e2e-${options.lane}`,
    lane: options.lane, platform,
    playwright_version: registry.playwright_version, registry_sha256: digest,
    qualification: options.qualification, collect_only: options.collectOnly,
    inventory_exit_code: null, execution_exit_code: null, runner_exit_code: 1, interrupted: false,
    inventory_sha256: null, execution_sha256: null, inventory_count: 0, execution_count: 0,
    completed_count: 0, status: 'failed', started_at: startedAt, finished_at: startedAt, duration_ms: 0,
  };
  let exitCode = 1;
  try {
    for (const phase of options.collectOnly ? ['inventory'] : ['inventory', 'execution']) {
      const filename = phase === 'inventory' ? inventoryPath : executionPath;
      const expected = laneTests(registry, options.lane, phase);
      const args = [cli, 'test', '--config', path.join(E2E_ROOT, 'playwright.config.mjs')];
      if (phase === 'inventory') args.push('--list');
      else if (options.lane !== 'legacy') args.push(...new Set(expected.map((test) => test.file)));
      const childEnv = childEnvironment(env, phase, options.lane, filename);
      const outcome = await runChild(args, childEnv, { lane: options.lane, phase, filename, expected });
      receipt[`${phase}_exit_code`] = outcome.code;
      receipt.interrupted ||= outcome.interrupted;
      if (existsSync(filename)) receipt[`${phase}_sha256`] = sha256(readFileSync(filename));
      if (outcome.code !== 0 || outcome.interrupted) throw new Error('browser_ci_child_failed');
      const verified = verifyEvents(readFileSync(filename, 'utf8'), {
        registry, digest, lane: options.lane, mode: phase, qualification: options.qualification,
      });
      receipt[`${phase}_count`] = verified.count;
      if (phase === 'execution') receipt.completed_count = verified.completed;
    }
    exitCode = 0; receipt.status = 'passed';
  } catch {
    // Do not echo exceptions, titles, paths, test data or Playwright results.
    console.error('browser-ci: failed or incomplete execution; inspect the existing console and restricted evidence');
  } finally {
    if (receipt.interrupted) receipt.status = 'interrupted';
    receipt.runner_exit_code = exitCode;
    receipt.finished_at = new Date().toISOString(); receipt.duration_ms = Math.round(performance.now() - started);
    writeFileSync(receiptPath, JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx', encoding: 'utf8' });
  }
  console.log(`browser-ci complete lane=${options.lane} status=${receipt.status} collected=${receipt.inventory_count} completed=${receipt.completed_count}`);
  return exitCode;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().then((code) => { process.exitCode = code; }).catch(() => {
    console.error('browser-ci: unable to complete restricted evidence'); process.exitCode = 1;
  });
}
