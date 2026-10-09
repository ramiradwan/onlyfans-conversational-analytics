import assert from 'node:assert/strict';
import { chromium, expect } from '@playwright/test';
import { readFile, writeFile, cp, mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { SyntheticPlatform } from '../../tools/e2e-capture/fixtures/synthetic-platform.mjs';
const variant = process.argv.includes('--normal-worker') ? 'normal-development' : 'read-only-release';
const root = await mkdtemp(path.join(tmpdir(), 'ofca-onboarding-observer-'));
const outputIndex = process.argv.indexOf('--output');
if (outputIndex !== -1 && !process.argv[outputIndex + 1]) throw new Error('--output requires a file path');
const output = outputIndex === -1 ? path.join(root, 'report.json') : path.resolve(process.argv[outputIndex + 1]);
const directory = path.join(root, 'extension');
await cp(fileURLToPath(new URL('../dist', import.meta.url)), directory, { recursive: true });
if (variant === 'normal-development') await build({ entryPoints: [fileURLToPath(new URL('../background.js', import.meta.url))],
  outfile: path.join(directory, 'background.js'), bundle: true, format: 'esm', platform: 'browser', target: ['chrome132'],
  sourcemap: false, minify: false });
const manifest = JSON.parse(await readFile(path.join(directory, 'manifest.json'), 'utf8'));
// Explicit platform-prompt substitution: only the optional site permission is
// pre-granted. Production worker, UI, storage, injection and navigation are real.
manifest.host_permissions.push('https://onlyfans.com/*');
await writeFile(path.join(directory, 'manifest.json'), JSON.stringify(manifest));
const bindings = JSON.parse(await readFile(new URL('../tests/fixtures/legal-instrument-bindings.synthetic.json', import.meta.url), 'utf8'));
const context = await chromium.launchPersistentContext(path.join(root, 'profile'), {
  channel: 'chromium', headless: true,
  args: [`--disable-extensions-except=${directory}`, `--load-extension=${directory}`],
});
const report = { variant, scope: 'Actual built MV3 worker and UI; synthetic OnlyFans network; optional permission pre-granted; synthetic legal release bindings; no Brain or hosted service. Normal development variant substitutes only its separately bundled source worker; read-only variant uses the audited dist worker.', checks: [], operations: [], root };
try {
  report.console = []; context.on('console', (message) => {
    if (!message.text().startsWith('The resource data:font')) report.console.push({ type: message.type(), text: message.text().slice(0, 300) });
  });
  const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
  const platform = new SyntheticPlatform({ activityDay: new Date(Date.now() - 86400000).toISOString().slice(0, 10) }); await platform.install(context);
  const id = new URL(worker.url()).hostname;
  await worker.evaluate(async (bindings) => {
    globalThis.__OFCA_TEST_LEGAL_RELEASE_BINDINGS__ = bindings;
    globalThis.browserOperations = [];
    for (const method of ['create', 'update', 'reload', 'remove']) {
      const original = chrome.tabs[method].bind(chrome.tabs);
      chrome.tabs[method] = (...args) => { browserOperations.push({ method, args }); return original(...args); };
    }
    globalThis.consentCommits = [];
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === 'local' && changes['ofca_consent_v1']) consentCommits.push({ at: Date.now(), mode: changes['ofca_consent_v1'].newValue?.mode });
    });
  }, bindings);
  const user = await context.newPage(); await user.goto('https://onlyfans.com/my/chats');
  const token = await user.evaluate(() => fixtureDocumentToken);
  const launcher = await context.newPage(); await launcher.goto(`chrome-extension://${id}/popup.html`);
  await launcher.waitForSelector('#journey-primary:not(.hidden)');
  const newSetup = context.waitForEvent('page'); await launcher.locator('#journey-primary').click();
  const setup = await newSetup;
  await setup.evaluate(() => {
    globalThis.renderedModes = [];
    new MutationObserver(() => {
      const text = document.getElementById('journey-title')?.textContent;
      if (text && renderedModes.at(-1)?.text !== text) renderedModes.push({ at: Date.now(), text });
    }).observe(document.querySelector('main'), { childList: true, subtree: true, attributes: true });
  });
  await setup.waitForSelector('#pre-mode:not(.hidden)');
  await setup.locator('#activate-software').click();
  assert.equal(await setup.locator('#terms-accepted').evaluate((node) => document.activeElement === node), true);
  await setup.locator('#terms-accepted').check(); await setup.locator('#risk-acknowledged').check();
  await setup.locator('#activate-software').click(); await setup.waitForSelector('#mode-choice:not(.hidden)');
  await setup.locator('#enable-preview').click();
  await setup.waitForSelector('#preview-metrics:not(.hidden)');
  await user.evaluate(() => fixtureRead('/api2/v2/users/me')); await user.evaluate(() => fixtureRead('/api2/v2/chats'));
  await user.evaluate(() => fixtureRead('/api2/v2/chats/fixture-peer-primary/messages'));
  const status = () => setup.evaluate(async () => (await chrome.runtime.sendMessage({ type: 'ofca.ui.status' })).status);
  await expect.poll(async () => (await status()).preview.message_observations, { timeout: 5000 }).toBeGreaterThan(0);
  const summary = await status();
  assert.equal(summary.consent.mode, 'preview'); assert.equal(summary.phase, 'preview');
  assert.equal(summary.brain_bound, false); assert.equal(summary.reload_required, false);
  report.checks.push('extension-only Preview without Brain or hosted identity');
  await launcher.locator('#journey-primary').click();
  assert.equal(context.pages().filter((page) => page.url().startsWith(`chrome-extension://${id}/setup.html`)).length, 1);
  report.checks.push('repeat workspace open reuses one tab');
  const before = await user.evaluate(() => ({ fetch: window.fetch, hook: globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.version }));
  assert.equal(before.hook, 2);
  await setup.locator('#pause').click();
  await expect.poll(async () => (await status()).consent.mode).toBe('paused');
  await setup.locator('#journey-primary').click();
  await expect.poll(async () => (await status()).consent.mode).toBe('preview');
  assert.equal(await user.evaluate(() => fixtureDocumentToken), token);
  report.checks.push('Preview pause/resume preserves user document');
  const helper = context.pages().find((page) => page.url() === 'https://onlyfans.com/');
  assert.ok(helper, 'silent existing document uses a single background helper');
  assert.equal((await status()).observer.helper, 'open');
  await helper.close();
  await expect.poll(async () => (await status()).observer.helper).toBe('closed');
  await setup.locator('#pause').click();
  await expect.poll(async () => (await status()).consent.mode).toBe('paused');
  await setup.locator('#journey-primary').click();
  await expect.poll(async () => (await status()).consent.mode).toBe('preview');
  assert.equal((await status()).observer.helper, 'closed');
  assert.equal(context.pages().filter((page) => page.url() === 'https://onlyfans.com/').length, 0);
  report.checks.push('deliberately closed helper stays closed through pause/resume');
  await setup.locator('#reopen-background-tab').click();
  await expect.poll(async () => (await status()).observer.helper).toBe('open');
  await expect.poll(() => context.pages().filter((page) => page.url() === 'https://onlyfans.com/').length).toBe(1);
  report.checks.push('explicit reopen creates one inactive replacement helper');
  const ownerResults = await setup.evaluate(async () => {
    const port = chrome.runtime.connect({ name: 'ofca.onboarding.v1' });
    let state, receive;
    const wait = (matches, send) => new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('owner_port_timeout')), 5000);
      receive = (value) => { if (matches(value)) { clearTimeout(timer); receive = null; resolve(value); } };
      send?.();
    });
    port.onMessage.addListener((value) => {
      if (value.profile === 'local-onboarding-state.v1') state = value;
      receive?.(value);
    });
    await wait((value) => value.profile === 'local-onboarding-state.v1');
    const results = [];
    for (const action of ['pause', 'resume']) {
      const operation_id = crypto.randomUUID();
      results.push(await wait((value) => value.profile === 'local-onboarding-result.v2' && value.operation_id === operation_id,
        () => port.postMessage({ type: 'command', epoch: state.epoch, command: { profile: 'local-onboarding-command.v1',
          owner: 'extension', action, journey_id: state.journey_id, operation_id,
          account_generation: state.account_generation, consent_generation: state.consent_generation } })));
    }
    const focused = await wait((value) => value.type === 'focus', () => port.postMessage({ type: 'focus' }));
    port.disconnect(); return { results, focused };
  });
  assert.deepEqual(ownerResults.results.map((value) => value.status), ['confirmed', 'confirmed']);
  assert.equal(ownerResults.results.every((value) => value.command_epoch === value.epoch), true);
  assert.deepEqual(ownerResults.focused, { type: 'focus', focused: true });
  report.checks.push('actual epoch-wrapped owner port confirms pause/resume and focuses the same workspace');
  await expect.poll(async () => (await status()).consent.mode).toBe('preview');
  await setup.locator('#journey-primary').click();
  await setup.waitForSelector('#full-disclosure:not(.hidden)');
  await setup.locator('#enable-full').click();
  await expect.poll(async () => (await status()).consent.mode).toBe('full');
  assert.equal((await status()).phase, 'identity');
  assert.equal((await status()).brain_bound, false);
  assert.equal(await user.evaluate(() => fixtureDocumentToken), token);
  assert.equal(await user.evaluate(() => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__?.mode), 'identity');
  report.checks.push('Preview-to-Full consent changes the existing observer to identity while desktop is absent');
  report.operations = await worker.evaluate(() => browserOperations);
  assert.equal(report.operations.filter((op) => op.method === 'reload').length, 0);
  const userId = await worker.evaluate(async () => (await chrome.tabs.query({ url: 'https://onlyfans.com/my/chats' }))[0].id);
  assert.equal(report.operations.some((op) => op.method === 'update' && op.args[0] === userId && op.args[1].url), false);
  const helpers = report.operations.filter((op) => op.method === 'create' && op.args[0].url === 'about:blank');
  assert.equal(helpers.length, 2); assert.equal(helpers.every((op) => op.args[0].active === false), true);
  report.checks.push('zero OnlyFans reloads, zero navigation of user document, only initial and explicit helper creation');
  report.commitMarks = await worker.evaluate(() => consentCommits);
  report.renderMarks = await setup.evaluate(() => renderedModes);
  report.localRenderLatencyMs = report.commitMarks.filter((value) => ['preview', 'paused'].includes(value.mode)).map((commit) => {
    const title = commit.mode === 'preview' ? 'Preview is ready' : 'Analytics paused';
    const rendered = report.renderMarks.find((mark) => mark.at >= commit.at && mark.text === title);
    assert.ok(rendered, `missing visible committed ${commit.mode} state`);
    return rendered.at - commit.at;
  });
  report.result = 'passed';
} catch (error) {
  report.pages = await Promise.all(context.pages().map(async (page) => ({ url: page.url(), text: await page.locator('body').innerText().catch(() => '') })));
  report.diagnostic = await (context.serviceWorkers()[0]?.evaluate(() => globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__?.()).catch((error) => String(error)));
  report.result = 'failed'; report.error = String(error.stack); throw error; }
finally {
  try {
    await writeFile(output, JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ result: report.result, checks: report.checks, artifact: directory, report: output }));
  }
  finally { await context.close(); }
}
