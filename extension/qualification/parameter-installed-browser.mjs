// Unchanged audited shipping background + frozen QA Brain; fixture controls
// operate outside both artifacts. No worker/background/manifest substitution.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { createServer, request as httpRequest } from 'node:http';
import { createServer as createSecureServer } from 'node:https';
import { readFile, readdir, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
import { createLineQueue, stopChild, within } from './snow-wasm-spike/process-lines.mjs';
import { realProducer } from './parameter-publication-producer.mjs';
import { platformBoundary } from './parameter-platform-fixture.mjs';
import { attachWorkerInspector } from './parameter-worker-inspector.mjs';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const args = new Map(process.argv.slice(2).map(value => { const i = value.indexOf('='); return [value.slice(0, i), value.slice(i + 1)]; }));
const required = key => { assert.ok(args.get(key), `required:${key}`); return path.resolve(args.get(key)); };
const frozen = required('--frozen'), extension = required('--extension'), directory = required('--outdir');
const signerRoot = required('--signer-root'), python = required('--python');
const variant = args.get('--variant') ?? 'continuity';
assert.ok(['continuity', 'expiry'].includes(variant));
const tls = JSON.parse(await readFile(required('--platform-tls-receipt')));
assert.equal(tls.qualification_only, true); assert.equal(tls.production_usable, false); assert.equal(tls.ca_installed, false);
await mkdir(directory, { recursive: true });
const data = path.join(directory, 'data'), profile = path.join(directory, 'profile');
const browserOptions = { channel: 'chromium', headless: true,
  args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`,
    '--host-resolver-rules=MAP onlyfans.com 127.0.0.1:18443', '--no-proxy-server',
    `--ignore-certificate-errors-spki-list=${tls.spki_sha256_base64}`] };
const sha = raw => createHash('sha256').update(raw).digest('hex');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let context, brain, lines, server, platformServer, worker, browserCdp, inspect, setup, stage = 'inputs';
let offline = false, workerRestarts = 0, brainRestarts = 0;
const peerFrames = [];
const publicationRequests = [], browserDistributionRequests = [], helperPageEvents = [], scope = {}, platform = platformBoundary();
const fixturePlatformPages = new WeakSet();
platform.setMarker('frontend-A');
const signedTotal = () => platform.counters.candidate + platform.counters.control + platform.counters.reads;
const budgetTuple = state => ({ used: state?.used, minute: state?.minute,
  days: Object.fromEntries(Object.entries(state?.days ?? {}).map(([day, value]) => [day,
    { automatic: value.automatic, requests: value.requests }])) });
const progress = setInterval(() => {
  void writeFile(path.join(directory, 'progress.json'), JSON.stringify({ stage }));
}, 2000); progress.unref();
const hashTree = async root => {
  const values = [];
  const walk = async (dir, prefix = '') => {
    for (const item of (await readdir(dir, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      const name = prefix + item.name, filename = path.join(dir, item.name);
      if (item.isDirectory()) await walk(filename, name + '/');
      else if (item.isFile()) values.push([name, sha(await readFile(filename))]);
      else throw new Error('artifact_alias_refused');
    }
  };
  await walk(root); return { sha256: sha(Buffer.from(JSON.stringify(values))), files: values.length };
};
const before = { brain: await hashTree(frozen), extension: await hashTree(extension) };
const componentHashes = { brain_executable: sha(await readFile(path.join(frozen, 'Brain.exe'))),
  extension_archive: sha(await readFile(path.join(extension, 'conversation-analytics-2.0.3.zip'))) };
const manifest = JSON.parse(await readFile(path.join(extension, 'manifest.json')));
assert.deepEqual(manifest.background, { service_worker: 'background.js', type: 'module' });
const producer = await realProducer(signerRoot);
await producer.publish('frontend-A');
server = createServer((request, response) => {
  publicationRequests.push(request.url);
  response.setHeader('Cache-Control', 'no-store');
  const relative = request.url?.replace(/^\/signing-parameters\/v1\//u, '');
  const raw = producer.files.get(relative);
  response.statusCode = offline ? 503 : raw ? 200 : 404;
  response.setHeader('Content-Type', relative?.endsWith('.jws') ? 'application/jose' : 'application/json');
  response.end(offline || !raw ? '' : raw);
});
await new Promise(resolve => server.listen(17873, '127.0.0.1', resolve));
platformServer = createSecureServer({ cert: await readFile(tls.certificate), key: await readFile(tls.private_key_path) },
  (request, response) => platform.handle(request, response));
await new Promise(resolve => platformServer.listen(18443, '127.0.0.1', resolve));
async function nextJson(timeout = 30_000) {
  const deadline = performance.now() + timeout;
  while (performance.now() < deadline) {
    const line = await lines.nextLine(Math.max(1, deadline - performance.now()));
    try { return JSON.parse(line); } catch { /* discard ordinary nonsecret diagnostics */ }
  }
  throw new Error('peer_readiness_timeout');
}
async function startBrain() {
  brain = spawn(path.join(frozen, 'Brain.exe'), [], { cwd: directory, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, LOCAL_ANALYTICS_DATA_DIR: data, PYTHONUTF8: '1' } });
  brain.stderr.setEncoding('utf8');
  brain.stderr.on('data', text => {
    for (const match of text.matchAll(/File "([^"]+)", line (\d+), in ([\w<>]+)/gu)) {
      peerFrames.push({ file: path.basename(match[1]), line: Number(match[2]), function: match[3] });
    }
  });
  lines = createLineQueue(brain, 'installed_peer');
  const ready = await nextJson();
  assert.equal(ready.ready, true);
  assert.equal(ready.production_checks, true);
  assert.equal(Object.keys(ready.activation).length, 6);
  assert.ok(Object.values(ready.activation).every(Boolean));
  return ready;
}
async function control(operation, value = {}) {
  brain.stdin.write(JSON.stringify({ operation, ...value }) + '\n');
  const result = await nextJson();
  assert.equal(result.error, undefined, `peer_refused:${operation}`);
  return result;
}
async function publicHealthStatus() {
  return new Promise((resolve, reject) => {
    const request = httpRequest({ hostname: '127.0.0.1', port: 17871, path: '/health',
      headers: { Host: 'bridge.localhost:17871' } }, response => {
      response.resume(); resolve(response.statusCode);
    });
    request.setTimeout(5000, () => request.destroy(new Error('public_health_timeout')));
    request.on('error', reject); request.end();
  });
}
async function installed(operation, value = {}) {
  if (operation === 'ready') return worker.evaluate(() => typeof globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__ === 'function');
  if (operation === 'wake') return true; // Wait for the owned production alarm; never force a coordinator.
  if (operation === 'snapshot') {
    const projection = await inspect(`(${(async function () {
      const runtime = agentRuntime, outbox = runtime.transport?.outbox;
      const storage = outbox?.storage;
      const record = storage ? await storage.runTransaction('readonly', ['credentials'], tx => tx.get('credentials', 'signer-state')) : null;
      const recovery = storage ? await storage.runTransaction('readonly', ['credentials'], tx => tx.get('credentials', 'signer-refresh-budget')) : null;
      const active = record?.state?.active;
      const jobs = outbox ? await outbox.historyJobs() : [];
      const allowance = jobs.find(job => job.job_id === 'catchup:control') ?? null;
      const publication = (await chrome.storage.local.get(['signing_publication_authority_v1'])).signing_publication_authority_v1;
      return {
        connected: companionClient.connected, consent: consentController.state.mode,
        generation: active ? { id: active.id, state_revision: record.state.state_revision, policy: active.proof_policy_version,
          revision: active.config.source_revision, document: active.summary.activation_document } : null,
        source: { persisted_catalog_sequence: publication?.catalog?.sequence ?? null }, jobs, allowance,
        history_error: runtime.history?.historyErrorCode?.() ?? null,
        policy_enabled: runtime.configuration?.activeDocument?.history_acquisition?.enabled ?? false,
        identity: outbox?.identityState(), recovery_attempts: recovery?.attempts ?? [],
        helper: consentController.observer.snapshot().helper,
        diagnostic: await globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__() };
  }).toString()})()`);
    return projection;
  }
  if (operation === 'legal') {
    for (const type of ['ofca.legal-activation.accept-terms', 'ofca.legal-activation.acknowledge-risk', 'ofca.legal-activation.activate-software']) {
      assert.equal((await setup.evaluate(type => chrome.runtime.sendMessage({ type }), type)).ok, true);
    }
    assert.equal((await setup.evaluate(() => chrome.runtime.sendMessage({ type: 'ofca.legal-activation.choose-mode', mode: 'full' }))).ok, true);
    return true;
  }
  if (operation === 'begin_pair') return setup.evaluate(() => {
    globalThis.qualificationPairState = null;
    globalThis.qualificationPairPort = chrome.runtime.connect({ name: 'ofca.companion.pairing' });
    globalThis.qualificationPairPort.onMessage.addListener(value => { if (value.state) globalThis.qualificationPairState = value; });
    globalThis.qualificationPairPort.postMessage({ type: 'pair' });
    return true;
  });
  if (operation === 'pair_status') return setup.evaluate(() => globalThis.qualificationPairState ?? { state: 'pending' });
  if (operation === 'finish_pair') {
    for (let i = 0; i < 100; i++) {
      if ((await installed('pair_status')).state === 'paired') return true;
      await delay(100);
    }
    return false;
  }
  if (operation === 'consent') {
    assert.equal((await setup.evaluate(mode => chrome.runtime.sendMessage({ type: 'ofca.ui.transition', mode }), value.mode)).ok, true);
    return true;
  }
  throw new Error('qualification_command_refused');
}
async function waitFor(label, predicate, timeout = 90_000, wake = false) {
  console.log(JSON.stringify({ progress: label }));
  const deadline = performance.now() + timeout;
  let last;
  while (performance.now() < deadline) {
    if (wake) await installed('wake');
    last = await installed('snapshot');
    if (predicate(last)) return last;
    await delay(1000);
  }
  // Closed public diagnostics only; no job records, account data, tokens or payloads.
  await writeFile(path.join(directory, 'failure-status.json'), JSON.stringify({ stage: label,
    connected: last?.connected, consent: last?.consent, source: last?.source,
    runtime_ready: last?.diagnostic?.runtimeReady, generation_policy: last?.generation?.policy,
    history_error: last?.history_error, policy_enabled: last?.policy_enabled,
    configuration: last?.diagnostic?.configuration, capture_phase: last?.diagnostic?.capturePhase,
    job_count: last?.jobs?.length,
    job_phases: last?.jobs?.slice(0, 20).map(job => ({ kind: job.kind, phase: job.phase, failure_code: job.failure_code,
      parameter_wait: job.parameter_wait?.reason })), platform_counters: platform.counters }, null, 2));
  throw new Error(`installed_wait_timeout:${label}`);
}
async function currentWorker() {
  worker = context.serviceWorkers().find(item => item.url().endsWith('/background.js'))
    ?? await context.waitForEvent('serviceworker', { timeout: 15_000 });
  for (let attempt = 0; attempt < 50; attempt++) {
    try {
      inspect = await attachWorkerInspector(browserCdp, worker.url());
      await inspect('typeof agentRuntime');
      return;
    } catch { await inspect?.close().catch(() => {}); inspect = null; }
    await delay(100);
  }
  throw new Error('installed_background_unavailable');
}
async function launchBrowser() {
  context = await chromium.launchPersistentContext(profile, browserOptions);
  context.on('request', request => {
    if (request.url().startsWith('http://127.0.0.1:17873/')) browserDistributionRequests.push(request.url());
  });
  context.on('page', page => {
    page.on('framenavigated', frame => {
      if (frame !== page.mainFrame()) return;
      if (frame.url().includes('/signing-helper.html')) helperPageEvents.push('packaged-signing-helper');
      if (frame.url().startsWith('https://onlyfans.com/') && !fixturePlatformPages.has(page)) helperPageEvents.push('owned-platform-helper');
    });
  });
  browserCdp = await context.browser().newBrowserCDPSession();
  await currentWorker();
}
async function restartWorker() {
  await inspect.close(); inspect = null;
  const serviceWorkerCdp = await context.newCDPSession(setup);
  const versions = new Map();
  const stoppedVersions = new Set();
  serviceWorkerCdp.on('ServiceWorker.workerVersionUpdated', ({ versions: next }) => {
    for (const item of next) {
      versions.set(item.versionId, item);
      if (item.runningStatus === 'stopped') stoppedVersions.add(item.versionId);
    }
  });
  try {
    await serviceWorkerCdp.send('ServiceWorker.enable');
    await delay(250);
    const version = [...versions.values()].find(item => item.scriptURL === worker.url() && item.runningStatus === 'running');
    assert.ok(version, 'actual_background_version_required');
    stoppedVersions.delete(version.versionId);
    await serviceWorkerCdp.send('ServiceWorker.stopWorker', { versionId: version.versionId });
    const stoppedDeadline = performance.now() + 15_000;
    while (!stoppedVersions.has(version.versionId) && performance.now() < stoppedDeadline) await delay(50);
    assert.equal(stoppedVersions.has(version.versionId), true, 'actual_stopped_transition_required');
    // Native traffic or the existing paired port may already have woken the
    // worker. Chromium retains its Target ID and Playwright Worker object
    // across a genuine stopped -> starting -> running context replacement.
    const page = context.pages().find(item => item.url().startsWith('chrome-extension://'));
    await page.evaluate(() => chrome.runtime.sendMessage({ type: 'ofca.ui.status' }).catch(() => {}));
    const runningDeadline = performance.now() + 15_000;
    while (versions.get(version.versionId)?.runningStatus !== 'running' && performance.now() < runningDeadline) await delay(50);
    assert.equal(versions.get(version.versionId)?.runningStatus, 'running', 'normal_worker_reactivation_required');
    workerRestarts++;
    scope.worker_termination_observed = true;
    scope.worker_reactivation_observed = true;
  } finally {
    await serviceWorkerCdp.detach();
  }
  await currentWorker();
}
const waitTuple = snapshot => snapshot.jobs.filter(job => job.parameter_wait).map(job => ({
  job_id: job.job_id, cursor: job.cursor, source_seq: snapshot.identity.last_source_seq,
  needed: job.parameter_wait.needed_revision, retries: job.retry_count ?? job.retries,
  grant: job.grant ? { check_id: job.check_id, kind: job.grant.kind, page_budget: job.grant.page_budget } : null,
  grant_used: job.grant_used, account_epoch: snapshot.identity.account_epoch,
}));
try {
  stage = 'fresh_admission';
  const admitted = spawn(python, ['-X', 'utf8', 'tools/signing-parameter-qualification/admit.py', data,
    ...(variant === 'expiry' ? ['--companion-authority-expires-in-seconds', '120'] : [])], {
    cwd: repository, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, LOCAL_ANALYTICS_DATA_DIR: data } });
  admitted.stderr.resume();
  const admittedLines = createLineQueue(admitted, 'admission');
  const receipt = JSON.parse(await admittedLines.nextLine(30_000));
  assert.equal(receipt.production_usable, false);
  assert.equal(receipt.verified_grant_types.length, 4);
  stage = 'frozen_startup';
  scope.activation = (await startBrain()).activation;
  stage = 'installed_background';
  await launchBrowser();
  const extensionId = new URL(worker.url()).host;
  await control('configure', { extension_id: extensionId });
  stage = 'normal_history_consent';
  await control('history_settings', { enabled: true });
  setup = await context.newPage(); await setup.goto(`chrome-extension://${extensionId}/setup.html`);
  const setupCdp = await context.newCDPSession(setup);
  stage = 'explicit_permissions';
  const permission = await within('optional_permissions', () => setupCdp.send('Runtime.evaluate', { expression:
    "chrome.permissions.request({permissions:['webRequest']})", userGesture: true,
    awaitPromise: true, returnByValue: true }), 15_000);
  assert.equal(permission.result.value, true);
  // Chrome's own extension settings UI grants runtime site access. This is
  // initial browser permission admission, never a manifest/profile override.
  const chromeSettings = await context.newPage(); await chromeSettings.goto('chrome://extensions/');
  await chromeSettings.evaluate(id => chrome.developerPrivate.addHostPermission(id, 'https://onlyfans.com/*'), extensionId);
  await chromeSettings.close();
  const hostPermission = await within('optional_host', () => setupCdp.send('Runtime.evaluate', { expression:
    "chrome.permissions.request({origins:['https://onlyfans.com/*']})", userGesture: true,
    awaitPromise: true, returnByValue: true }), 15_000);
  assert.equal(hostPermission.result.value, true);
  assert.equal(await setup.evaluate(() => chrome.permissions.contains({ permissions: ['webRequest'], origins: ['https://onlyfans.com/*'] })), true);
  scope.initial_permission_admission = 'Chrome-extension-settings-UI-API-and-normal-optional-permission-API';
  // Current Chromium's unpacked optional-permission network factory activates
  // on browser startup. Qualify parameter delivery after one initial setup
  // restart, before any pairing, native customer document or signing proof.
  stage = 'initial_permission_browser_restart';
  await inspect.close(); inspect = null; await context.close(); context = null;
  await launchBrowser();
  assert.equal(new URL(worker.url()).host, extensionId);
  setup = await context.newPage(); await setup.goto(`chrome-extension://${extensionId}/setup.html`);
  assert.equal(await setup.evaluate(() => chrome.permissions.contains({ permissions: ['webRequest'], origins: ['https://onlyfans.com/*'] })), true);
  scope.initial_permission_browser_restart = 1;
  const platformPage = await context.newPage(); fixturePlatformPages.add(platformPage); await platformPage.goto('https://onlyfans.com/');
  stage = 'ordinary_legal_actions'; await installed('legal');
  await delay(1500);
  stage = 'normal_pairing'; await control('open'); await installed('begin_pair');
  let pairing;
  const deadline = performance.now() + 15_000;
  do {
    pairing = await installed('pair_status');
    if (pairing.state === 'compare') break;
    await delay(100);
  } while (performance.now() < deadline);
  assert.match(pairing.comparison_code ?? '', /^\d{6}$/u);
  const comparison = await control('status'); assert.equal(comparison.comparison_code, pairing.comparison_code);
  await control('confirm', { comparison_code: pairing.comparison_code });
  assert.equal(await installed('finish_pair'), true);
  stage = 'initial_policy3_A';
  const first = await waitFor(stage, state => state.generation?.policy === 3 && state.generation.revision === 'frontend-A'
    && platform.counters.reads > 0, 90_000, true);
  assert.equal(platform.counters.control, 1);
  const generationA = first.generation;
  if (variant === 'expiry') {
    assert.ok(Number.isSafeInteger(receipt.companion_authority_expires_at));
    stage = 'real_companion_authority_expiry';
    while (Date.now() < receipt.companion_authority_expires_at * 1000) await delay(1000);
    await waitFor(stage, state => !state.connected && !state.diagnostic.sessionBound, 20_000);
    const readsAfterFence = signedTotal();
    await delay(2000);
    assert.equal(signedTotal(), readsAfterFence);
    const refused = await control('resume');
    assert.equal(refused.refused, true);
    assert.equal((await control('state')).grant_status, 'refused');
    assert.equal(await publicHealthStatus(), 200);
    scope.real_companion_authority_expiry = true;
    scope.brain_healthy_after_authority_expiry = true;
  } else {
  stage = 'A_same_document_backend_B';
  const backendReads = platform.counters.reads, initialProof = platform.counters.control;
  const initialCandidate = platform.counters.candidate, initialPublications = publicationRequests.length;
  const initialRecovery = first.recovery_attempts, initialHelpers = helperPageEvents.length;
  platform.setMarker('backend-B');
  const backendB = await waitFor(stage, state => platform.counters.reads > backendReads, 90_000, true);
  assert.deepEqual(backendB.generation, generationA);
  assert.equal(platform.counters.control, initialProof);
  assert.equal(platform.counters.candidate, initialCandidate);
  assert.equal(publicationRequests.length, initialPublications);
  assert.deepEqual(backendB.recovery_attempts, initialRecovery);
  assert.equal(backendB.helper, 'none');
  assert.equal(helperPageEvents.length, initialHelpers);
  scope.same_document_backend_B = true;
  platform.setMarker(null);
  stage = 'same_document_missing_backend_marker';
  const reads = platform.counters.reads, proof = platform.counters.control;
  const unchanged = await waitFor(stage, state => platform.counters.reads > reads, 90_000, true);
  assert.deepEqual(unchanged.generation, generationA);
  assert.equal(platform.counters.control, proof);
  assert.equal(platform.counters.candidate, initialCandidate);
  assert.equal(publicationRequests.length, initialPublications);
  assert.deepEqual(unchanged.recovery_attempts, initialRecovery);
  assert.equal(unchanged.helper, 'none');
  assert.equal(helperPageEvents.length, initialHelpers);
  scope.same_document_missing_backend_marker = true;
  scope.backend_marker_preserves_generation_checkpoint_and_recovery_budget = true;
  scope.backend_marker_starts_no_owned_helper = true;
  stage = 'new_document_B_missing';
  platform.setRevision('frontend-B'); platform.setMarker('backend-independent-C');
  await platformPage.goto('https://onlyfans.com/new-frontend-B');
  const missing = await waitFor(stage, state => state.jobs.some(job => job.parameter_wait?.needed_revision === 'frontend-B'), 90_000, true);
  assert.equal(platform.counters.revisions['frontend-B'] ?? 0, 0);
  assert.deepEqual(missing.recovery_attempts, initialRecovery);
  assert.equal(missing.helper, 'none');
  assert.equal(helperPageEvents.length, initialHelpers);
  const durableWait = waitTuple(missing), allowance = budgetTuple(missing.allowance);
  scope.new_document_B_wait = true;
  stage = 'worker_termination_with_wait'; await restartWorker();
  const restored = await waitFor(stage, state => waitTuple(state).length > 0, 15_000);
  assert.deepEqual(waitTuple(restored), durableWait);
  assert.deepEqual(budgetTuple(restored.allowance), allowance);
  scope.worker_termination_wait = true;
  stage = 'Brain_restart_with_wait';
  await control('stop'); await stopChild(brain, 'installed_peer'); brain = null;
  await startBrain(); brainRestarts++;
  await control('configure', { extension_id: extensionId });
  const brainRestored = await waitFor(stage, state => state.connected && waitTuple(state).length > 0, 30_000, true);
  assert.deepEqual(waitTuple(brainRestored), durableWait);
  assert.deepEqual(budgetTuple(brainRestored.allowance), allowance);
  assert.deepEqual(brainRestored.recovery_attempts, initialRecovery);
  scope.brain_restart_wait = true;
  stage = 'real_publication_B_arrival';
  const published = await producer.publish('frontend-B', { withdraw: ['frontend-A'] });
  assert.equal(published.sequence, 2);
  const cursorCountBeforeArrival = platform.counters.cursors.length;
  const resumed = await waitFor(stage, state => state.generation?.revision === 'frontend-B'
    && !state.jobs.some(job => job.parameter_wait?.needed_revision === 'frontend-B')
    && platform.counters.revisions['frontend-B'] > 2, 90_000, true);
  assert.equal(resumed.generation.policy, 3);
  assert.notEqual(resumed.generation.document, generationA.document);
  assert.equal(resumed.identity.account_epoch, missing.identity.account_epoch);
  for (const waited of durableWait) {
    const continued = resumed.jobs.find(job => job.job_id === waited.job_id);
    assert.ok(continued, 'waiting_job_survives_publication');
    if (waited.grant) assert.deepEqual({ check_id: continued.check_id, kind: continued.grant.kind,
      page_budget: continued.grant.page_budget }, waited.grant);
  }
  const inventoryWait = durableWait.find(job => job.cursor !== undefined && job.cursor !== null);
  if (inventoryWait) assert.equal(platform.counters.cursors[cursorCountBeforeArrival], Number(inventoryWait.cursor));
  assert.deepEqual(resumed.recovery_attempts, initialRecovery);
  assert.equal(resumed.helper, 'none');
  assert.equal(helperPageEvents.length, initialHelpers);
  assert.ok(platform.counters.cursors.some(cursor => cursor > 0));
  scope.real_publication_B_resume = true;
  scope.dependency_wait_preserves_grants_cursor_and_page_allowance = true;
  let currentBGeneration = resumed.generation;
  stage = 'wrong_platform_account';
  platform.setAccount('67890');
  await waitFor(stage, state => !state.connected, 20_000);
  const wrongAccountReads = signedTotal();
  await delay(1500);
  assert.equal(signedTotal(), wrongAccountReads);
  scope.wrong_account_refusal = true;
  platform.setAccount('12345');
  await platformPage.goto('https://onlyfans.com/user-restored-account-B');
  const restoredAccount = await waitFor('ordinary_account_restore', state => state.connected
    && state.generation?.revision === 'frontend-B' && state.generation.document !== currentBGeneration.document, 90_000);
  currentBGeneration = restoredAccount.generation;
  stage = 'pause_fence';
  await installed('consent', { mode: 'pause' });
  const pausedReads = signedTotal(); await delay(1500);
  assert.equal(signedTotal(), pausedReads);
  assert.equal((await installed('snapshot')).consent, 'paused');
  await installed('consent', { mode: 'resume' });
  scope.pause_resume = true;
  stage = 'offline_cached_publications'; offline = true;
  await control('stop'); await stopChild(brain, 'installed_peer'); brain = null;
  await startBrain(); brainRestarts++;
  await control('configure', { extension_id: extensionId });
  const cachedReads = platform.counters.reads;
  const cached = await waitFor(stage, state => state.connected && platform.counters.reads > cachedReads, 90_000, true);
  assert.equal(cached.generation.revision, 'frontend-B');
  scope.offline_valid_cache = true; offline = false;
  stage = 'damaged_disposable_object';
  await control('stop'); await stopChild(brain, 'installed_peer'); brain = null;
  const currentCatalog = JSON.parse(Buffer.from(producer.files.get('catalog.jws').toString().split('.')[1], 'base64url'));
  const objectEntry = currentCatalog.rules.find(entry => entry.source_revision === 'frontend-B');
  const objectPath = `objects/sha256/${objectEntry.sha256}.json`;
  const cachedObject = path.join(data, 'signing-parameter-cache', `${objectEntry.sha256}.json`);
  await writeFile(cachedObject, Buffer.from('corrupted-disposable-public-object'));
  const downloadsBefore = publicationRequests.filter(url => url.endsWith(objectPath)).length;
  const readsBeforeDamageRestart = platform.counters.reads;
  await startBrain(); brainRestarts++;
  await control('configure', { extension_id: extensionId });
  const damageSurvived = await waitFor('valid_worker_cache_survives_damaged_Brain_object', state => state.connected
    && state.generation?.revision === 'frontend-B' && platform.counters.reads > readsBeforeDamageRestart, 90_000);
  assert.equal(damageSurvived.generation.id, cached.generation.id);
  scope.damaged_disposable_object_never_replaces_verified_worker_bytes = true;
  if (publicationRequests.filter(url => url.endsWith(objectPath)).length === downloadsBefore) {
    // The worker's independently verified object remains eligible. A pending
    // background repair grants no reason to invalidate it or to force a page
    // reload. Reconstruction creates an ordinary resolver need after the
    // bounded negative interval; no private source/coordinator method is used.
    await delay(31_000);
    await restartWorker();
  }
  const repaired = await waitFor(stage, state => state.connected && state.generation?.revision === 'frontend-B'
    && publicationRequests.filter(url => url.endsWith(objectPath)).length > downloadsBefore, 90_000);
  assert.equal(repaired.generation.id, cached.generation.id);
  assert.equal(sha(await readFile(cachedObject)), objectEntry.sha256);
  scope.damaged_disposable_object_refetched = true;
  stage = 'same_document_B_withdrawal';
  await producer.publish('unused', { empty: true });
  await control('stop'); await stopChild(brain, 'installed_peer'); brain = null;
  await delay(1500); // Let the ordinary session-lost fence settle entered work.
  await startBrain(); brainRestarts++;
  await control('configure', { extension_id: extensionId });
  const withdrawn = await waitFor(stage, state => state.source.persisted_catalog_sequence === 3
    && state.jobs.some(job => job.parameter_wait?.needed_revision === 'frontend-B'), 90_000, true);
  assert.equal(withdrawn.generation.document, currentBGeneration.document);
  await delay(1500);
  const withdrawalFence = await installed('snapshot');
  const withdrawnReads = signedTotal(), withdrawnTuple = waitTuple(withdrawalFence);
  const withdrawnAllowance = budgetTuple(withdrawalFence.allowance);
  await delay(32_000); // Includes an ordinary owned dependency retry window.
  assert.equal(signedTotal(), withdrawnReads);
  const withdrawalRetry = await installed('snapshot');
  assert.equal(withdrawalRetry.source.persisted_catalog_sequence, 3);
  assert.deepEqual(waitTuple(withdrawalRetry), withdrawnTuple);
  assert.deepEqual(budgetTuple(withdrawalRetry.allowance), withdrawnAllowance);
  assert.deepEqual(withdrawalRetry.recovery_attempts, withdrawalFence.recovery_attempts);
  assert.equal(withdrawalRetry.helper, 'none');
  scope.withdrawal_refusal = true;
  }
  assert.deepEqual(browserDistributionRequests, []);
  const after = { brain: await hashTree(frozen), extension: await hashTree(extension) };
  assert.deepEqual(after, before);
  const result = { schema: 'ofca-installed-signing-parameter-qualification/v1', result: 'passed',
    scope: 'frozen-QA-Brain-normal-app-lifecycle-and-unchanged-production-background', variant,
    production_usable: false, tpm_hardware_qualified: false, webauthn_ceremony_qualified: false,
    qualification_zip_acceptance: true, shipping_background_installed: true, shipping_zip_acceptance: false,
    fixture_endpoint_compiled: true, runtime_origin_override: false,
    artifacts: before, after, component_sha256: componentHashes,
    worker_restarts: workerRestarts, brain_restarts: brainRestarts,
    producer_source: producer.receipt, browser: context.browser().version(),
    checks: scope, complementary_variant_required: variant === 'expiry' ? 'continuity' : 'expiry',
    normal_grants_verified: receipt.verified_grant_types,
    native_capture: 'actual-Chrome-webRequest-browser-document-metadata',
    fixture_platform_native_poll_ms: 500, forced_customer_reload: false,
    platform_boundary: 'actual-loopback-TLS-synthetic-platform-with-isolated-browser-DNS-and-SPKI',
    platform_certificate_sha256: tls.certificate_sha256,
    consumer_public_network_requests: browserDistributionRequests.length, producer_path_requests: publicationRequests.length,
    observed_owned_helper_page_events: helperPageEvents.length };
  await writeFile(path.join(directory, 'report.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result));
} catch (error) {
  const result = { schema: 'ofca-installed-signing-parameter-qualification/v1', result: 'failed', stage,
    artifacts: before, checks: scope, production_usable: false,
    error_class: error?.name ?? 'Error',
    ...(String(error?.message).startsWith('Installed ') ? { inspector_failure: error.message } : {}),
    peer_frames: peerFrames.slice(-12), frames: String(error?.stack ?? '').split('\n').filter(line =>
      /parameter-installed-browser\.mjs:\d+:\d+/u.test(line)).map(line => line.trim()) };
  await writeFile(path.join(directory, 'report.json'), JSON.stringify(result, null, 2) + '\n');
  console.error(JSON.stringify(result));
  process.exitCode = 1;
} finally {
  clearInterval(progress);
  await inspect?.close();
  await context?.close();
  if (brain) { try { await control('stop'); } catch {} await stopChild(brain, 'installed_peer'); }
  await new Promise(resolve => server.close(resolve));
  await new Promise(resolve => platformServer.close(resolve));
}
