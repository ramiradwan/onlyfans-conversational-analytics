import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createBrowserTaskOwner, attachInstalledHelpers } from '../runtime/browser-task-owner.mjs';
import { payload, id } from '../test-fixtures/browser-task.mjs';
import { NATIVE_LAUNCH_KEY } from '../runtime/onboarding-native-launch.mjs';
import { isObservation } from '../../shared/browser-tasks/wire.mjs';

function harness() {
  const local = {}, session = {}, calls = { identity: 0, opened: 0, creator: 0 };
  const draft_scope = { scope_id: id(7), disclosure_bundle_id: 'b'.repeat(64) };
  let record = null, account = 'creator-a', profile = null;
  const consent = { consent: { mode: 'off', consent_epoch: 'epoch-1' }, onlyfans_permission: false };
  const legal = { configured: true, requires_reauthorization: false };
  const storage = data => ({ async get(key) { return { [key]: data[key] }; }, async set(value) { Object.assign(data, value); } });
  const chromeApi = { runtime: { id: 'a'.repeat(32) }, storage: { local: storage(local), session: storage(session) } };
  const owner = createBrowserTaskOwner({ chromeApi, onboarding: {
    async browserTaskContext(journey) { if (record && record.journey_id !== journey) throw Error('task_changed'); return { record, draft_scope }; },
    async openBrowserTask(task) { calls.opened++; record = { journey_id: task.journey_id, draft_scope }; },
  }, consentController: { async status() { return consent; } }, legalActivationController: { async status() { return legal; } },
  identityBridge: { async currentAccountId() { calls.identity++; return account; }, async currentProfile() { return profile; } }, openCreatorAccount: () => { calls.creator++; } });
  return { owner, consent, legal, local, session, calls, draft_scope, setRecord: value => { record = value; }, setAccount: value => { account = value; },
    setProfile: value => { profile = value; } };
}

test('desktop-first attachment never grants consent, observes an account, or opens a tab', async () => {
  const h = harness(), context = await h.owner.attach(payload());
  assert.equal((await h.owner.read(context)).state, 'needs-consent');
  assert.deepEqual(h.calls, { identity: 0, opened: 0, creator: 0 });
  await h.owner.open(context, async () => {});
  assert.deepEqual(h.calls, { identity: 0, opened: 1, creator: 0 });
  assert.equal(h.consent.consent.mode, 'off');
});
test('permitted account projection needs current legal consent and site permission', async () => {
  const h = harness(), context = await h.owner.attach(payload());
  h.consent.consent.mode = 'full';
  assert.equal((await h.owner.read(context)).state, 'needs-permission');
  assert.equal(h.calls.identity, 0);
  h.consent.onlyfans_permission = true; h.legal.requires_reauthorization = true;
  assert.equal((await h.owner.read(context)).creator_account_id, null);
  assert.equal(h.calls.identity, 0);
  h.legal.requires_reauthorization = false;
  const ready = await h.owner.read(context);
  assert.equal(ready.state, 'ready'); assert.equal(ready.creator_account_id, 'creator-a');
  h.setAccount('creator-b');
  await assert.rejects(h.owner.read(context), /task_changed/);
});
test('extension-first requires saved Full launch evidence in the exact profile and draft', async () => {
  const h = harness(), value = payload(); value.entry_source = 'extension';
  await assert.rejects(h.owner.attach(value), /task_changed/);
  h.setRecord({ journey_id: value.task.journey_id, draft_scope: h.draft_scope });
  await assert.rejects(h.owner.attach(value), /task_changed/);
  h.session[NATIVE_LAUNCH_KEY] = { journey_id: value.task.journey_id, draft_scope: h.draft_scope, phase: 'pending', expires_at: Date.now() + 5000 };
  const context = await h.owner.attach(value);
  assert.equal((await h.owner.read(context)).state, 'needs-consent');
  delete h.session[NATIVE_LAUNCH_KEY];
  assert.ok(await h.owner.attach(value), 'saved adoption survives the short launch intent');
  h.setRecord({ journey_id: id(55) });
  await assert.rejects(h.owner.attach(value), /task_changed/);
});
test('a task pinned by Brain cannot attach to another instance or draft', async () => {
  const h = harness(), value = payload();
  value.participant = { instance_id: id(99), draft_scope_id: h.draft_scope.scope_id, disclosure_bundle_id: h.draft_scope.disclosure_bundle_id };
  await assert.rejects(h.owner.attach(value), /profile_mismatch/);
});

test('task labels are display-only and require the same current permitted account', async () => {
  const h = harness(), context = await h.owner.attach(payload());
  h.setProfile({ creator_account_id: 'creator-a', display_name: 'Synthetic Creator', username: 'synthetic_handle' });
  assert.equal((await h.owner.read(context)).creator_display_name, undefined);
  h.consent.consent.mode = 'full'; h.consent.onlyfans_permission = true;
  const observed = await h.owner.read(context);
  assert.equal(observed.creator_display_name, 'Synthetic Creator');
  assert.equal(isObservation(observed), true);
  for (const invalid of ['x'.repeat(81), 'a\nb', '\u202eevil', ' padded ', null]) {
    assert.equal(isObservation({ ...observed, creator_display_name: invalid }), false);
  }
  assert.equal(isObservation({ ...observed, creator_account_id: null, state: 'needs-account' }), false);
  h.setProfile({ creator_account_id: 'creator-b', display_name: 'Synthetic Creator' });
  await assert.rejects(h.owner.read(context), /task_changed/);
});

test('shared observation schema and runtime agree on bounded label syntax', async () => {
  const schema = JSON.parse(await readFile(new URL('../../shared/browser-tasks/schema.json', import.meta.url), 'utf8'));
  const h = harness(), context = await h.owner.attach(payload());
  h.consent.consent.mode = 'full'; h.consent.onlyfans_permission = true;
  const observed = await h.owner.read(context);
  for (const field of ['creator_display_name', 'creator_username']) {
    const rule = schema.$defs.observation.properties[field];
    for (const label of ['Creator Name', '🙂'.repeat(rule.maxLength), 'a\u2028b', '', 'a\u2028\u202eb',
      '\u0085text', ' padded ', '\ufeffpadded', 'x'.repeat(rule.maxLength + 1), null]) {
      const accepted = typeof label === 'string' && [...label].length >= rule.minLength
        && [...label].length <= rule.maxLength && new RegExp(rule.pattern, 'u').test(label);
      assert.equal(isObservation({ ...observed, [field]: label }), accepted);
    }
  }
});
test('an account-bound task rejects a different creator before authentication can open', async () => {
  const h = harness(), value = payload(); value.creator_account_id = 'creator-b';
  h.consent.consent.mode = 'full'; h.consent.onlyfans_permission = true;
  const context = await h.owner.attach(value);
  await assert.rejects(h.owner.read(context), /account_mismatch/u);
  assert.equal(h.calls.opened, 0);
});

test('paused authenticated tasks use the pinned account without a platform identity read', async () => {
  const h = harness(), value = payload(); value.creator_account_id = 'creator-a';
  h.consent.consent = { mode: 'paused', resume_mode: 'full', consent_epoch: 'epoch-1' };
  h.consent.onlyfans_permission = true;
  const context = await h.owner.attach(value);
  const result = await h.owner.readPaired(context, 'creator-a');
  assert.equal(result.state, 'paused'); assert.equal(result.creator_account_id, 'creator-a');
  assert.equal(h.calls.identity, 0);
  h.legal.requires_reauthorization = true;
  await assert.rejects(h.owner.readPaired(context, 'creator-a'), /task_changed/u);
  assert.equal(h.calls.identity, 0);
});
test('install arrival refreshes only exact helper URLs without closing or focusing user tabs', async () => {
  const helper = 'http://bridge.localhost:17871/browser-tasks#presentation=' + id(4), scripts = [];
  const chromeApi = { tabs: { async query() { return [
    { id: 1, url: helper }, { id: 2, url: 'https://onlyfans.com/' }, { id: 3, url: helper + '&extra=1' },
  ]; } }, scripting: { async executeScript(value) { scripts.push(value); } } };
  assert.equal(await attachInstalledHelpers(chromeApi), true);
  assert.equal(scripts.length, 1); assert.deepEqual(scripts[0].target, { tabId: 1, frameIds: [0] });
  let reloads = 0;
  const run = Function('window', 'location', `return (${scripts[0].func})(...${JSON.stringify(scripts[0].args)})`);
  const window = {}; window.top = window;
  run(window, { href: 'https://onlyfans.com/', reload() { reloads++; } });
  assert.equal(reloads, 0, 'a navigated tab is not touched');
  run(window, { href: helper, reload() { reloads++; } }); assert.equal(reloads, 1);
});
