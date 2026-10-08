// Actual pinned Chromium, synthetic in-memory content only. No app, Vite,
// Brain, account, persistent profile, screenshots, traces, or external network.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { CaptureRecorder, sourceIdentity } from './recording.mjs';
import { generateInventory } from './inventory.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../../..');
const SECRET = 'visual_ci_synthetic_private_391827';
const scenarios = ['page-error', 'unknown-id', 'unterminated', 'cancelled', 'write-failure', 'crashed'];
const incomplete = new Set(['unterminated', 'cancelled', 'crashed']);
const stages = new Set(['setup', 'launch', 'connect', 'page-create', 'page-content', 'page-error', 'cancel-start',
  'cancel-close', 'cancel-reject', 'crash-start', 'crash-kill', 'crash-disconnect', 'crash-reject',
  'recorder', 'progress', 'inventory', 'receipt', 'publisher', 'outputs', 'cleanup-browser', 'cleanup-directory']);
let activeScenario = null;
let activeStage = 'setup', failureStage = null;

async function bounded(promise, code, milliseconds = 5000) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(code)), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

// BrowserServer owns precisely this ephemeral browser. Its public close/kill
// methods wait for termination; also join the exposed ChildProcess close event
// before the host workload guard is allowed to release its lease.
async function closeOwnedBrowser(owner) {
  if (!owner) return;
  try {
    await bounded(Promise.all([owner.server.close(), owner.closed]), 'visual_ci_probe_close_timeout');
  } catch {
    await bounded(Promise.all([owner.server.kill(), owner.closed]), 'visual_ci_probe_kill_timeout');
  }
}

async function filesUnder(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const filename = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await filesUnder(filename));
    else files.push(filename);
  }
  return files;
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length && (args.length !== 2 || args[0] !== '--python')) throw new Error('visual_ci_invalid_probe_arguments');
  const python = args[1] ?? process.env.VISUAL_CI_PYTHON ?? 'python';
  const packageInfo = JSON.parse(await readFile(resolve(here, '../node_modules/playwright/package.json'), 'utf8'));
  assert.equal(packageInfo.version, '1.63.0');
  const source = sourceIdentity({ ...process.env, GITHUB_RUN_ID: process.env.GITHUB_RUN_ID ?? '1' }, root);
  const directory = await mkdtemp(join(tmpdir(), 'visual-ci-sentinel-'));
  let browser, owner;
  try {
    activeStage = 'launch';
    const server = await chromium.launchServer({ headless: true, host: '127.0.0.1', port: 0 });
    const child = server.process();
    const closed = new Promise(done => {
      if (child.exitCode !== null || child.signalCode !== null) done();
      else child.once('close', done);
    });
    owner = { server, closed };
    activeStage = 'connect';
    browser = await chromium.connect(server.wsEndpoint());
    const definition = generateInventory().cases.find(value => value.group === 'dynamic');
    for (const scenario of scenarios) {
      activeScenario = scenario;
      const raw = join(directory, scenario, 'raw'); const published = join(directory, scenario, 'diagnostics');
      const summary = join(directory, scenario, 'probe-summary.md');
      await mkdir(raw, { recursive: true });
      activeStage = 'page-create';
      const page = await browser.newPage();
      await page.route('**/*', route => route.abort());
      let failure;
      try {
        activeStage = 'page-content';
        await page.setContent('<main><h1>Synthetic capture probe</h1></main>');
        activeStage = 'page-error';
        try { await page.evaluate(value => { throw new Error(value); }, SECRET); }
        catch (error) { failure = error; }
        if (scenario === 'cancelled') {
          activeStage = 'cancel-start';
          const pending = page.evaluate(() => { window.__syntheticPending = true; return new Promise(() => {}); }).then(() => false, () => true);
          await page.waitForFunction(() => window.__syntheticPending === true, null, { timeout: 5000 });
          activeStage = 'cancel-close';
          await bounded(page.close(), 'visual_ci_probe_cancel_timeout');
          activeStage = 'cancel-reject';
          assert.equal(await bounded(pending, 'visual_ci_probe_cancel_rejection_timeout'), true);
        } else if (scenario === 'crashed') {
          activeStage = 'crash-start';
          const disconnected = new Promise(done => browser.once('disconnected', done));
          const pending = page.evaluate(() => { window.__syntheticPending = true; return new Promise(() => {}); }).then(() => false, () => true);
          await page.waitForFunction(() => window.__syntheticPending === true, null, { timeout: 5000 });
          // Force an actual process failure while a request is in flight. Unlike
          // Page.crash notifications, termination is independently observable
          // on Windows too and does not depend on renderer crash reporting.
          activeStage = 'crash-kill';
          await bounded(Promise.all([owner.server.kill(), owner.closed]), 'visual_ci_probe_crash_exit_timeout');
          activeStage = 'crash-disconnect';
          await bounded(disconnected, 'visual_ci_probe_disconnect_timeout');
          assert.equal(browser.isConnected(), false);
          activeStage = 'crash-reject';
          assert.equal(await bounded(pending, 'visual_ci_probe_crash_rejection_timeout'), true);
        }
      } finally {
        if (browser.isConnected()) await bounded(page.close(), 'visual_ci_probe_page_close_timeout').catch(() => {});
      }
      assert(failure?.message.includes(SECRET));
      activeStage = 'recorder';
      let clock = 0;
      const recorder = new CaptureRecorder({ group: 'dynamic', source, clock: () => clock });
      if (scenario === 'unknown-id') {
        assert.throws(() => recorder.begin(failure.message, {}), { message: 'visual_ci_invalid_case_start' });
      } else {
        const finish = recorder.begin(definition.id, definition.configuration);
        if (!incomplete.has(scenario)) finish({ outcome: failure.message, observations: [failure.message], files: [`${SECRET}.png`] });
      }
      clock = 60_000;
      activeStage = 'progress';
      const progress = recorder.progress('dynamic');
      assert(!progress.includes(SECRET)); assert(progress.includes('elapsed_seconds=60'));
      activeStage = 'inventory';
      await recorder.writeInventory(raw);
      activeStage = 'receipt';
      if (scenario === 'write-failure') {
        await mkdir(join(raw, 'capture-ci.json'));
        await assert.rejects(recorder.finish(raw, { exitCode: 1, phaseTimings: { dynamic: 60 } }), { message: 'visual_ci_receipt_write_failed' });
      } else {
        const receipt = await recorder.finish(raw, { exitCode: 1, phaseTimings: { dynamic: 60 } });
        assert.equal(receipt.complete, false);
      }
      activeStage = 'publisher';
      const result = spawnSync(python, [join(root, 'tools/ci_visual_gate.py'), 'diagnostics', '--artifact-dir', raw, '--diagnostics-dir', published], {
        cwd: root, encoding: 'utf8', windowsHide: true, timeout: 30_000,
        env: { ...process.env, PRODUCT_SHA: source.source_commit, GITHUB_RUN_ID: source.workflow_run_id,
          GITHUB_RUN_ATTEMPT: String(source.run_attempt), GITHUB_STEP_SUMMARY: summary },
      });
      assert.equal(result.status, 0);
      assert(!String(result.stdout).includes(SECRET)); assert(!String(result.stderr).includes(SECRET));
      activeStage = 'outputs';
      const output = await filesUnder(join(directory, scenario));
      assert(output.every(file => !/\.(png|zip|webm|har)$/i.test(file)));
      for (const file of output) assert(!(await readFile(file, 'utf8')).includes(SECRET));
      assert((await readFile(summary, 'utf8')).includes('Visual capture'));
      const publishedFiles = await readdir(published);
      assert.deepEqual(publishedFiles, ['diagnostics.json']);
      const diagnostics = JSON.parse(await readFile(join(published, 'diagnostics.json'), 'utf8'));
      assert.equal(diagnostics.status, scenario === 'write-failure' ? 'metadata_unavailable' : 'restricted_metadata');
      assert.deepEqual(diagnostics.cases, ['unknown-id', 'write-failure'].includes(scenario) ? []
        : [{ id: definition.id, outcome: incomplete.has(scenario) ? 'incomplete' : 'failed' }]);
    }
  } catch (error) {
    failureStage = activeStage;
    throw error;
  } finally {
    activeStage = 'cleanup-browser';
    try { await closeOwnedBrowser(owner); }
    catch (error) { failureStage ??= activeStage; throw error; }
    finally {
      activeStage = 'cleanup-directory';
      if (dirname(directory) !== tmpdir() || !basename(directory).startsWith('visual-ci-sentinel-')) throw new Error('visual_ci_probe_cleanup_boundary');
      await rm(directory, { recursive: true, force: true });
    }
  }
  console.log(JSON.stringify({ schema: 'visual-ci-sentinels/v1', playwright_version: packageInfo.version, cases: scenarios.length, passed: true }));
}

main().catch(() => {
  const stage = failureStage ?? activeStage;
  console.error(`visual-ci synthetic output safety probe failed case=${scenarios.includes(activeScenario) ? activeScenario : 'setup'} stage=${stages.has(stage) ? stage : 'setup'}`);
  process.exitCode = 1;
});
