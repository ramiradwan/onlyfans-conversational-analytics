import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import test from 'node:test';
import { build } from 'esbuild';
import { payload, id } from '../test-fixtures/browser-task.mjs';

test('the packaged worker routes a creator click without changing settings', async () => {
  const buildSource = await readFile(new URL('../build.mjs', import.meta.url), 'utf8');
  const entry = buildSource.match(/entryPoints: \[path.join\(ROOT, '(background[^']+)'\)\]/)[1];
  const compiled = await build({ entryPoints: [fileURLToPath(new URL(`../${entry}`, import.meta.url))], bundle: true, write: false, format: 'esm', platform: 'browser' });
  assert.match(compiled.outputFiles[0].text, /createBrowserTaskOwner\(\{[^}]*authentication: browserAuth[^}]*\}\)/u);
  // Evaluate a closed bundle so renamed imports remain the production code.
  const navigation = await build({ stdin: { contents: `
    export { createBrowserTaskOwner } from './runtime/browser-task-owner.mjs';
    export { openCreatorAccount } from './ui/actions.mjs';`,
    resolveDir: fileURLToPath(new URL('../', import.meta.url)) },
  bundle: true, write: false, format: 'iife', globalName: 'navigation', platform: 'browser' });
  const { createBrowserTaskOwner, openCreatorAccount } = runInNewContext(navigation.outputFiles[0].text + '\nnavigation;', { crypto, Date, TextEncoder, URL });
  for (const existing of [true, false]) {
    const calls = [];
    const chromeApi = { tabs: {
      query: async () => existing ? [{ id: 7, windowId: 2 }] : [],
      update: async (id, options) => calls.push(['focus', id, options]),
      create: async (options) => calls.push(['create', options]),
    }, windows: { update: async (id, options) => calls.push(['window', id, options]) } };
    const storage = {};
    const scope = { scope_id: id(7), disclosure_bundle_id: 'b'.repeat(64) };
    const owner = createBrowserTaskOwner({ chromeApi: { ...chromeApi, runtime: { id: 'a'.repeat(32) }, storage: { local: {
      async get(key) { return { [key]: storage[key] }; }, async set(value) { Object.assign(storage, value); } } } },
      onboarding: { async browserTaskContext() { return { record: null, draft_scope: scope }; },
        async openBrowserTask() { calls.push(['surface', { section: 'access' }]); } },
      consentController: { async status() { return { consent: { mode: 'full', consent_epoch: 'epoch' }, onlyfans_permission: true }; } },
      legalActivationController: { async status() { return { configured: true, requires_reauthorization: false }; } },
      identityBridge: { async currentAccountId() { return null; } }, openCreatorAccount: () => openCreatorAccount(chromeApi) });
    const context = await owner.attach(payload());
    assert.deepEqual(calls, []);
    await owner.open(context, async () => {});
    assert.equal(calls[0][0], existing ? 'focus' : 'create');
    assert.equal(calls.length, existing ? 2 : 1);
  }
});
