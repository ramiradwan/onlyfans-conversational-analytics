import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { BROWSER_AUTH_KEY, createBrowserAuth, parseBrowserAuthReturn } from '../runtime/browser-auth.mjs';
import { payload, id } from '../test-fixtures/browser-task.mjs';

const ORIGIN = 'https://provider.example';
const RETURN = 'https://' + 'a'.repeat(32) + '.chromiumapp.org/browser-ceremony/v1/complete';
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function request(now, index = 1) {
  const value = payload(now);
  value.task.task_id = id(index);
  value.creator_account_id = 'synthetic-creator';
  value.task.purpose = 'browser.authentication'; value.task.operation_id = id(index + 50);
  value.task.expires_at = now / 1000 + 120;
  value.ceremony = { entry_url: ORIGIN + '/onboarding/browser-ceremonies/' + 'A'.repeat(43),
    ceremony_id: `0199b234-5678-7000-8000-${String(index).padStart(12, '0')}`, attempt_id: 'A'.repeat(43),
    completion: { mode: 'extension-auth-flow', return_uri: RETURN }, expires_at: new Date(now + 120000).toISOString() };
  return value;
}
function callback(value, notification = 'changed') {
  return `${RETURN}?ceremony_id=${value.ceremony.ceremony_id}&attempt_id=${value.ceremony.attempt_id}&notification=${notification}`;
}
function harness(t) {
  let clock = Date.now();
  const stored = {}, flows = [], listeners = new Set(), alarms = new Map();
  const chromeApi = { runtime: { id: 'a'.repeat(32) }, storage: { local: {
    async get(key) { return structuredClone({ [key]: stored[key] }); },
    async set(value) { Object.assign(stored, structuredClone(value)); },
  } }, alarms: { async create(name, value) { alarms.set(name, value); }, async clear(name) { alarms.delete(name); },
    onAlarm: { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn) } },
  identity: { getRedirectURL: path => `https://${'a'.repeat(32)}.chromiumapp.org/${path}`,
    launchWebAuthFlow(options) { const result = { ...deferred(), options }; flows.push(result); return result.promise; } } };
  const make = () => { const owner = createBrowserAuth({ chromeApi, hostedOrigin: ORIGIN, now: () => clock }); t.after(() => owner.dispose()); return owner; };
  return { owner: make(), make, stored, flows, alarms, chromeApi, request: index => request(clock, index),
    advance(ms) { clock += ms; }, async alarm() { for (const fn of listeners) fn({ name: 'browser-auth-expiry' }); await tick(); } };
}

for (const vector of JSON.parse(readFileSync(new URL('../../contracts/browser-ceremony-v1/callback-cases.json', import.meta.url), 'utf8'))) {
  test('callback contract: ' + vector.case_id, () => {
    const ceremony = { completion: { return_uri: vector.return_uri }, ceremony_id: vector.ceremony_id, attempt_id: vector.attempt_id };
    if (vector.valid) assert.equal(parseBrowserAuthReturn(vector.url, ceremony).ceremony_id, vector.ceremony_id);
    else assert.throws(() => parseBrowserAuthReturn(vector.url, ceremony), /authentication_unconfirmed/u);
  });
}

test('reads never launch, duplicate actions reuse a journaled flow, and only correlation is retained', async t => {
  const h = harness(t), value = h.request();
  assert.deepEqual(await h.owner.read(value), { state: 'idle', notification: null });
  assert.equal(h.flows.length, 0);
  let gates = 0;
  const guard = async () => { gates++; };
  const results = await Promise.all([h.owner.launch(value, guard), h.owner.launch(value, guard)]);
  assert.ok(results.every(result => result.state === 'opening'));
  assert.equal(h.flows.length, 1); assert.equal(gates, 3);
  assert.deepEqual(h.flows[0].options, { interactive: true, url: value.ceremony.entry_url });
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'opening');
  assert.equal(h.alarms.get('browser-auth-expiry').when, value.task.expires_at * 1000);
  h.flows[0].resolve(callback(value)); await tick();
  const completed = await h.owner.read(value);
  assert.equal(completed.state, 'checking'); assert.equal(completed.notification.notification, 'changed');
  assert.deepEqual(Object.keys(completed.notification).sort(), ['attempt_id', 'ceremony_id', 'notification']);
  await h.owner.launch(value, guard); assert.equal(h.flows.length, 1);
  assert.equal(JSON.stringify(h.stored).includes('https:'), false);
});

test('rejects unpinned entries and redirect URIs before calling Chrome', async t => {
  const h = harness(t);
  for (const change of [
    value => { value.ceremony.entry_url = value.ceremony.entry_url.replace(ORIGIN, 'https://evil.example'); },
    value => { value.ceremony.entry_url += '?continue=https://evil.example'; },
    value => { value.ceremony.entry_url = value.ceremony.entry_url.replace('https:', 'http:'); },
    value => { value.ceremony.entry_url = value.ceremony.entry_url.replace('provider.example', 'user@provider.example'); },
    value => { value.ceremony.completion.return_uri += '/other'; },
    value => { value.ceremony.completion.return_uri = value.ceremony.completion.return_uri.replace('a'.repeat(32), 'b'.repeat(32)); },
    value => { value.ceremony.completion = { mode: 'system-browser' }; },
    value => { value.ceremony.attempt_id = 'B'.repeat(43); },
    value => { value.task.purpose = 'browser.access'; },
    value => { value.extension_id = 'b'.repeat(32); },
    value => { value.task.expires_at = 0; },
  ]) {
    const value = h.request(); change(value);
    await assert.rejects(h.owner.launch(value, async () => {}));
  }
  assert.equal(h.flows.length, 0);
});

for (const mode of ['failure', 'bad-callback', 'shutdown']) {
  test(`${mode} is an unknown outcome and reopening requires an explicit action`, async t => {
    const h = harness(t), value = h.request();
    await h.owner.launch(value, async () => {});
    let owner = h.owner;
    if (mode === 'shutdown') { owner.dispose(); h.alarms.clear(); owner = h.make(); }
    else if (mode === 'failure') h.flows[0].reject(Error('Sensitive API diagnostic must not be retained'));
    else h.flows[0].resolve(callback(value) + '&id_token=forbidden');
    await tick();
    const state = await owner.read(value);
    assert.equal(state.notification.notification, 'unavailable'); assert.equal(h.flows.length, 1);
    assert.equal(h.alarms.get('browser-auth-expiry').when, value.task.expires_at * 1000);
    assert.equal(JSON.stringify(h.stored).includes('Sensitive'), false);
    assert.equal(JSON.stringify(h.stored).includes('id_token'), false);
    await owner.launch(value, async () => {}); assert.equal(h.flows.length, 2);
    if (mode === 'shutdown') {
      h.flows[0].resolve(callback(value)); await tick();
      assert.equal((await owner.read(value)).state, 'opening');
    }
  });
}

test('window closure is a durable local observation and never reopens the same child', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {});
  h.flows[0].reject(Error('The user did not approve access.')); await tick(); await tick();
  assert.deepEqual(await h.owner.read(value), { state: 'closed', notification: null });
  assert.equal(h.owner.hasLiveFlow(), false);
  h.owner.dispose(); const reopened = h.make();
  assert.deepEqual(await reopened.launch(value, async () => {}), { state: 'closed', notification: null });
  assert.equal(h.flows.length, 1);
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'closed');
  assert.equal(JSON.stringify(h.stored).includes('approve access'), false);
  assert.equal(JSON.stringify(h.stored).includes('https:'), false);
});

test('synchronous launch failure cannot be interpreted as window closure', async t => {
  const h = harness(t), value = h.request();
  h.chromeApi.identity.launchWebAuthFlow = () => { throw Error('The user did not approve access.'); };
  await h.owner.launch(value, async () => {}); await tick(); await tick();
  assert.equal((await h.owner.read(value)).state, 'checking');
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'unknown');
});

test('only admitted preparation recovery with a fresh generation reopens a closed child once', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {});
  h.flows[0].reject(Error('The user did not approve access.')); await tick(); await tick();
  const retry = structuredClone(value); retry.task.admission_generation = id(99);
  assert.deepEqual(await h.owner.launch(retry, async () => {}), { state: 'closed', notification: null });
  retry.ceremony.preparation_recovery = false;
  await assert.rejects(h.owner.launch(retry, async () => {}), /authentication_unavailable/);
  retry.ceremony.preparation_recovery = true;
  retry.task.admission_generation = value.task.admission_generation;
  assert.deepEqual(await h.owner.launch(retry, async () => {}), { state: 'closed', notification: null });
  retry.task.admission_generation = id(99);
  assert.deepEqual(await h.owner.read(retry), { state: 'idle', notification: null });
  assert.equal(h.flows.length, 1, 'a read never opens recovery');
  await assert.rejects(h.owner.launch(retry, async () => { throw Error('task_changed'); }), /task_changed/);
  await h.owner.launch(retry, async () => {});
  await h.owner.launch(retry, async () => {});
  assert.equal(h.flows.length, 2);
  assert.equal(h.flows[1].options.url, value.ceremony.entry_url);
  h.flows[1].reject(Error('The user did not approve access.')); await tick(); await tick();
  await h.owner.launch(retry, async () => {});
  assert.equal(h.flows.length, 2);
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].closed_generation, id(99));
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].expires_at, value.task.expires_at * 1000);
});

test('reissued helper can read the same ceremony but a changed scope cannot reuse it', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {});
  const replacement = structuredClone(value); replacement.task.admission_generation = id(20);
  assert.equal((await h.owner.launch(replacement, async () => {})).state, 'opening');
  assert.equal(h.flows.length, 1);
  replacement.task.scope_digest = 'b'.repeat(64);
  await assert.rejects(h.owner.launch(replacement, async () => {}), /task_changed/u);
  h.flows[0].resolve(callback(value)); await tick();
  assert.equal((await h.owner.read(value)).notification.notification, 'changed');
});

test('successor authentication tasks share their parent ceremony without sharing outcomes', async t => {
  const h = harness(t), first = h.request(), successor = structuredClone(first);
  successor.task.task_id = id(2); successor.task.operation_id = id(52);
  successor.task.scope_digest = 'b'.repeat(64); successor.task.source_revision++;
  await h.owner.launch(first, async () => {});
  h.flows[0].reject(Error('cancelled')); await tick(); await tick();
  assert.equal((await h.owner.read(first)).notification.notification, 'unavailable');
  assert.deepEqual(await h.owner.read(successor), { state: 'idle', notification: null });
  await h.owner.launch(successor, async () => {});
  assert.equal(h.flows.length, 2);
  assert.equal((await h.owner.read(successor)).state, 'opening');
  h.flows[1].resolve(callback(successor)); await tick(); await tick();
  assert.equal((await h.owner.read(successor)).notification.notification, 'changed');
  assert.equal((await h.owner.read(first)).notification.notification, 'unavailable');
  const restarted = h.make();
  assert.equal((await restarted.read(successor)).notification.notification, 'changed');
  assert.equal((await restarted.read(first)).notification.notification, 'unavailable');
  assert.equal(h.stored[BROWSER_AUTH_KEY].length, 2);
});

test('a late callback changes only its exact task while its successor remains active', async t => {
  const h = harness(t), first = h.request(), successor = structuredClone(first);
  successor.task.task_id = id(2); successor.task.operation_id = id(52);
  await h.owner.launch(first, async () => {});
  await h.owner.launch(successor, async () => {});
  h.flows[0].resolve(callback(first, 'retired')); await tick(); await tick();
  assert.equal((await h.owner.read(first)).notification.notification, 'retired');
  assert.equal((await h.owner.read(successor)).state, 'opening');
  assert.equal(h.owner.hasLiveFlow(), true);
  h.flows[1].reject(Error('closed')); await tick(); await tick();
  assert.equal(h.owner.hasLiveFlow(), false);
});

test('the same task cannot change ceremony, operation, participant or original expiry', async t => {
  const h = harness(t), first = h.request();
  await h.owner.launch(first, async () => {});
  for (const change of [
    value => { value.task.operation_id = id(90); },
    value => { value.ceremony.ceremony_id = '0199b234-5678-7000-8000-000000000099'; },
    value => { value.ceremony.attempt_id = 'C'.repeat(42) + 'A'; },
    value => { value.task.expires_at -= 1; },
    value => { value.creator_account_id = 'other-creator'; },
  ]) {
    const changed = structuredClone(first); change(changed);
    await assert.rejects(h.owner.read(changed), /task_changed/);
    await assert.rejects(h.owner.launch(changed, async () => {}), /task_changed/);
  }
  assert.equal(h.flows.length, 1);
});

test('rechecks current owner after journaling and does not open after revocation', async t => {
  const h = harness(t), value = h.request();
  let gates = 0;
  await assert.rejects(h.owner.launch(value, async () => { if (++gates === 2) throw Error('task_changed'); }), /task_changed/u);
  assert.equal(h.flows.length, 0);
  assert.equal((await h.owner.read(value)).notification.notification, 'unavailable');
});

test('fixed expiry discards late callbacks and bounds retained correlations without evicting live work', async t => {
  const h = harness(t), requests = Array.from({ length: 9 }, (_, index) => h.request(index + 1));
  for (const value of requests.slice(0, 8)) await h.owner.launch(value, async () => {});
  await assert.rejects(h.owner.launch(requests[8], async () => {}), /authentication_unavailable/u);
  assert.equal(h.flows.length, 8); assert.equal(h.stored[BROWSER_AUTH_KEY].length, 8);
  h.advance(120001); await h.alarm();
  h.flows[0].resolve(callback(requests[0])); await tick();
  assert.deepEqual(h.stored[BROWSER_AUTH_KEY], []);
  await assert.rejects(h.owner.read(requests[0]), /task_expired/u);
  await h.owner.launch(h.request(9), async () => {}); assert.equal(h.flows.length, 9);
});

test('corrupt journals cannot silently restart an uncertain attempt', async t => {
  const h = harness(t);
  h.stored[BROWSER_AUTH_KEY] = [{ phase: 'opening', entry_url: 'https://evil.example' }];
  await assert.rejects(h.owner.launch(h.request(), async () => {}), /authentication_unavailable/u);
  assert.equal(h.flows.length, 0);
});

test('persisted ceremony correlation cannot disagree with the exact task fingerprint', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {});
  h.stored[BROWSER_AUTH_KEY][0].ceremony_id = '0199b234-5678-7000-8000-000000000099';
  await assert.rejects(h.owner.read(value), /task_changed/);
  await assert.rejects(h.owner.launch(value, async () => {}), /task_changed/);
  assert.equal(h.flows.length, 1);
});

test('flow ownership covers asynchronous preparation and the window lifetime, but retains unknown history separately', async t => {
  const h = harness(t), value = h.request(), gate = deferred();
  const pending = h.owner.launch(value, () => gate.promise);
  assert.equal(h.owner.hasLiveFlow(), true, 'reservation exists before asynchronous preparation');
  await tick(); assert.equal(h.flows.length, 0);
  gate.resolve(); await pending;
  assert.equal(h.owner.hasLiveFlow(), true, 'Chrome remains active after launch returns');
  h.flows[0].reject(Error('cancelled')); await tick(); await tick();
  assert.equal(h.owner.hasLiveFlow(), false);
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'unknown');
  assert.equal((await h.owner.read(value)).notification.notification, 'unavailable');
});

test('failed preparation and original expiry release flow ownership without a new deadline', async t => {
  const h = harness(t), value = h.request();
  await assert.rejects(h.owner.launch(value, async () => { throw Error('task_changed'); }), /task_changed/);
  assert.equal(h.owner.hasLiveFlow(), false);
  await h.owner.launch(value, async () => {});
  h.advance(120001);
  assert.equal(h.owner.hasLiveFlow(), false);
  assert.equal(h.flows.length, 1);
});
