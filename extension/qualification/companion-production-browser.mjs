import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
import { build } from 'esbuild';
import { createLineQueue, stopChild, waitForWorkerEntry, within } from './snow-wasm-spike/process-lines.mjs';
import '../test-fixtures/pairing/vendored-vector.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here), repository = path.dirname(root);
const temporary = await mkdtemp(path.join(tmpdir(), 'ofca-production-companion-'));
const extension = path.join(temporary, 'extension'), profile = path.join(temporary, 'profile');
let context, child, stage = 'initialize', launchNumber = 0;
let workerEntryStarted = null;
let readinessEpoch;
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
try {
  await cp(path.join(root, 'dist'), extension, { recursive: true });
  const originalManifest = await readFile(path.join(extension, 'manifest.json'));
  const manifest = JSON.parse(originalManifest);
  const wasm = await readFile(path.join(extension, 'ofca_snow_wasm_bg.wasm'));
  await build({ stdin: {
    resolveDir: here,
    contents: `import * as scenario from './companion-production-scenario.mjs';
      globalThis.productionScenario=(operation,value)=>scenario[operation](value);
      chrome.runtime.onMessage.addListener(()=>{});`,
  }, bundle: true, format: 'esm', outfile: path.join(extension, 'production-qualification.mjs') });
  await writeFile(path.join(extension, 'manifest.json'), JSON.stringify({ ...manifest,
    background: { service_worker: 'production-qualification.mjs', type: 'module' } }));
  if (process.argv.includes('--readiness-epoch')) {
    await build({ stdin: {
      resolveDir: here,
      contents: `import { createSurfaceClient } from '../ui/surface-client.mjs';
        const client = createSurfaceClient(() => {}, () => {});
        globalThis.readinessObservation = () => client.model.analysisReadiness.commercial_authority;
        void client.start();`,
    }, bundle: true, format: 'esm', outfile: path.join(extension, 'qualification-surface.mjs') });
    await writeFile(path.join(extension, 'popup.html'), '<!doctype html><script type="module" src="qualification-surface.mjs"></script>');
  }
  child = spawn(process.env.PYTHON ?? 'python', ['tools/companion-session-qualification/serve.py'], {
    cwd: repository, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  });
  // Application diagnostics are deliberately not reflected into test failures.
  // Fixed operation labels identify failures without echoing grant/secret data.
  child.stderr.resume();
  const lines = createLineQueue(child, 'production_peer');
  const ready = JSON.parse(await lines.nextLine(30_000));
  assert.equal(ready.ready, true);
  async function control(operation, value = {}) {
    child.stdin.write(JSON.stringify({ operation, ...value }) + '\n');
    const result = JSON.parse(await lines.nextLine(10_000));
    if (result.error) throw new Error('production_peer_refused');
    return result;
  }
  const browser = process.argv[2];
  async function launch() {
    launchNumber += 1;
    stage = 'browser_launch';
    context = await chromium.launchPersistentContext(profile, {
      ...(browser && browser !== 'playwright' ? { executablePath: browser } : { channel: 'chromium' }),
      headless: true, args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
    });
    const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker', { timeout: 15_000 });
    stage = 'worker_entry';
    workerEntryStarted = performance.now();
    await waitForWorkerEntry(worker, 'productionScenario');
    workerEntryStarted = null;
    stage = 'peer_configuration';
    await control('configure', { extension_id: new URL(worker.url()).host });
    stage = 'fixture_preparation';
    await worker.evaluate((value) => globalThis.productionScenario('prepare', value), {
      account: ready.account, now: ready.fixture_clock,
      trust: JSON.parse(await readFile(path.join(repository, 'contracts/companion-pairing-v1/trust-set.json'))),
    });
    return worker;
  }
  stage = 'pairing';
  let worker = await launch();
  stage = 'pairing_window';
  await control('open');
  stage = 'pairing_request';
  await worker.evaluate(() => globalThis.productionScenario('beginPairing'));
  const deadline = performance.now() + 10_000;
  let comparison;
  stage = 'pairing_offer';
  while (performance.now() < deadline) {
    comparison = await worker.evaluate(() => globalThis.productionScenario('comparison'));
    if (comparison.failed) throw new Error('production_pairing_refused');
    if (comparison.state === 'compare') break;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.match(comparison.comparison_code ?? '', /^[0-9]{6}$/u);
  stage = 'pairing_confirmation';
  let status;
  do {
    status = await control('status');
    if (status.state === 'awaiting_confirmation') break;
    if (performance.now() >= deadline) throw new Error('production_confirmation_timeout');
    await new Promise((resolve) => setTimeout(resolve, 20));
  } while (true);
  assert.equal(comparison.comparison_code, status.comparison_code);
  await control('confirm', { comparison_code: comparison.comparison_code });
  assert.equal((await worker.evaluate(() => globalThis.productionScenario('finishPairing'))).paired, true);
  stage = 'protected_operations';
  const first = await within(stage, () => worker.evaluate(() => globalThis.productionScenario('exercise')), 20_000);
  assert.ok(Object.values(first).every((value) => value === true));
  const browserVersion = context.browser().version();
  await context.close(); context = null;
  stage = 'browser_restart';
  worker = await launch();
  const restored = await within(stage, () => worker.evaluate(() => globalThis.productionScenario('exercise')), 20_000);
  assert.ok(Object.values(restored).every((value) => value === true));
  stage = 'inbound_fragment_burst';
  await worker.evaluate(() => globalThis.productionScenario('listenForBurst'));
  await control('inbound_burst');
  const inbound = await worker.evaluate(() => globalThis.productionScenario('receiveBurst'));
  assert.equal(inbound.inbound_512k, true);
  if (process.argv.includes('--readiness-epoch')) {
    stage = 'readiness_consent_epoch';
    await worker.evaluate(() => globalThis.productionScenario('startReadiness'));
    const page = await context.newPage();
    await page.goto(`chrome-extension://${new URL(worker.url()).host}/popup.html`);
    const count = () => worker.evaluate(() => globalThis.productionScenario('readinessCount'));
    const waitForReads = number => within(stage, async () => {
      while (await count() < number) await new Promise(resolve => setTimeout(resolve, 10));
    }, 5_000);
    await waitForReads(1);
    assert.equal(await worker.evaluate(() => globalThis.productionScenario('advanceReadinessConsent')), true);
    await waitForReads(2);
    await worker.evaluate(() => globalThis.productionScenario('releaseReadiness', 0));
    // Let Chrome deliver the held response while request B is still held.
    await page.waitForTimeout(100);
    const stale = await page.evaluate(() => globalThis.readinessObservation()) !== 'unknown';
    await worker.evaluate(() => globalThis.productionScenario('releaseReadiness', 1));
    await page.waitForFunction(() => globalThis.readinessObservation() !== 'unknown');
    readinessEpoch = { both_requests_reached_brain: true, obsolete_reply_rendered: stale };
    await page.close();
    assert.equal(stale, false);
  }
  stage = 'revocation';
  await control('revoke');
  const revocation = await within(stage, () => worker.evaluate(() => globalThis.productionScenario('revoked')), 15_000);
  assert.deepEqual(revocation, { closed: true, refused: true });
  const result = {
    schema: 'ofca-production-companion-qualification/v1', result: 'passed',
    fixture: 'pinned-reconstructable-contract', fixture_clock: ready.fixture_clock,
    browser: browserVersion, manifest_sha256: digest(originalManifest), wasm_sha256: digest(wasm),
    native: ready.native, pairing_code_compared: true, explicit_confirmation: true,
    first_connection: first, browser_restart: restored, ...inbound, revocation,
    ...(readinessEpoch ? { readiness_epoch: readinessEpoch } : {}),
  };
  if (process.argv[3]) await writeFile(process.argv[3], JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result));
} catch (error) {
  // Retain only a closed set of harness failure labels, never exception text
  // from production code or fixture records.
  const message = typeof error?.message === 'string' ? error.message : '';
  const code = message.includes('worker_initialization_timeout') ? 'worker_initialization_timeout'
    : message.includes('production_peer_line_timeout') ? 'production_peer_line_timeout'
      : message === 'production_peer_refused' ? 'production_peer_refused' : 'qualification_operation_failed';
  const result = {
    schema: 'ofca-production-companion-qualification/v1', result: 'failed',
    stage, code, launch_number: launchNumber,
    ...(readinessEpoch ? { readiness_epoch: readinessEpoch } : {}),
    ...(workerEntryStarted === null ? {} : { worker_entry_elapsed_ms: Math.round(performance.now() - workerEntryStarted) }),
  };
  if (process.argv[3]) await writeFile(process.argv[3], JSON.stringify(result, null, 2) + '\n');
  console.error(JSON.stringify(result));
  throw new Error(`production_companion_qualification_failed:${stage}:${code}`);
} finally {
  await context?.close();
  await stopChild(child, 'production_peer');
  if (!path.resolve(temporary).startsWith(path.resolve(tmpdir()) + path.sep)) throw new Error('unsafe_temporary_path');
  await rm(temporary, { recursive: true, force: true });
}
