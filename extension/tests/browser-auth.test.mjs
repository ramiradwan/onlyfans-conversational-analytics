import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
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
const event = () => { const listeners = new Set();
  return { listeners, addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
    emit: (...args) => { for (const fn of [...listeners]) fn(...args); } }; };
const WINDOW_BASE = 917331000, TAB_BASE = 828441000;
function harness(t) {
  let clock = Date.now();
  const stored = {}, flows = [], listeners = new Set(), alarms = new Map(), removed = [], focused = [];
  const onUpdated = event(), onRemoved = event();
  const control = { createFailure: null, createGate: null };
  const chromeApi = { runtime: { id: 'a'.repeat(32) }, storage: { local: {
    async get(key) { return structuredClone({ [key]: stored[key] }); },
    async set(value) { Object.assign(stored, structuredClone(value)); },
  } }, alarms: { async create(name, value) { alarms.set(name, value); }, async clear(name) { alarms.delete(name); },
    onAlarm: { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn) } },
  identity: { getRedirectURL: path => `https://${'a'.repeat(32)}.chromiumapp.org/${path}` },
  tabs: { onUpdated },
  windows: {
    async create(options) {
      const windowId = WINDOW_BASE + flows.length, tabId = TAB_BASE + flows.length;
      const flow = { options, windowId, tabId,
        navigate(url, { window = windowId, tab = tabId } = {}) { onUpdated.emit(tab, { url }, { id: tab, windowId: window, url }); },
        close() { onRemoved.emit(windowId); } };
      flows.push(flow);
      if (control.createGate) await control.createGate;
      if (control.createFailure) throw control.createFailure;
      return { id: windowId, type: 'popup', tabs: [{ id: tabId, windowId }] };
    },
    async remove(windowId) { removed.push(windowId); onRemoved.emit(windowId); },
    async update(windowId, options) { focused.push([windowId, options]); return { id: windowId }; },
    onRemoved,
  } };
  const make = () => { const owner = createBrowserAuth({ chromeApi, hostedOrigin: ORIGIN, now: () => clock }); t.after(() => owner.dispose()); return owner; };
  return { owner: make(), make, stored, flows, alarms, chromeApi, removed, focused, control, onUpdated, onRemoved,
    request: index => request(clock, index),
    advance(ms) { clock += ms; }, async alarm() { for (const fn of listeners) fn({ name: 'browser-auth-expiry' }); await tick(); } };
}
const RECORD_KEYS = ['attempt_id', 'ceremony_id', 'closed_generation', 'expires_at', 'fingerprint', 'notification', 'phase', 'sequence', 'task_id'];
function assertBoundedJournal(h) {
  const journal = JSON.stringify(h.stored[BROWSER_AUTH_KEY] ?? []);
  for (const forbidden of ['http', 'chromiumapp', 'provider.example', 'evil', 'popup', 'system-browser',
    ...h.flows.flatMap(flow => [String(flow.windowId), String(flow.tabId)])]) {
    assert.equal(journal.includes(forbidden), false, `journal contains ${forbidden}`);
  }
  for (const record of h.stored[BROWSER_AUTH_KEY] ?? []) {
    assert.ok(Object.keys(record).every(key => RECORD_KEYS.includes(key)), Object.keys(record).join());
  }
}
// A record an earlier build journaled after Chrome's return.
function journalReturn(h, notification) {
  Object.assign(h.stored[BROWSER_AUTH_KEY][0], { phase: 'returned', notification });
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
  assert.deepEqual(h.flows[0].options, { type: 'popup', url: value.ceremony.entry_url + '#system-browser', focused: true, width: 500, height: 720 });
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'opening');
  assert.equal(h.alarms.get('browser-auth-expiry').when, value.task.expires_at * 1000);
  journalReturn(h, 'changed');
  const restarted = h.make();
  const completed = await restarted.read(value);
  assert.equal(completed.state, 'checking'); assert.equal(completed.notification.notification, 'changed');
  assert.deepEqual(Object.keys(completed.notification).sort(), ['attempt_id', 'ceremony_id', 'notification']);
  await restarted.launch(value, guard); assert.equal(h.flows.length, 1);
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

for (const mode of ['failure', 'task-ended', 'shutdown']) {
  test(`${mode} is an unknown outcome and reopening requires an explicit action`, async t => {
    const h = harness(t), value = h.request();
    if (mode === 'failure') h.control.createFailure = Error('Sensitive API diagnostic must not be retained');
    await h.owner.launch(value, async () => {});
    let owner = h.owner;
    if (mode === 'shutdown') { owner.dispose(); h.alarms.clear(); owner = h.make(); }
    else if (mode === 'failure') h.control.createFailure = null;
    else { h.flows[0].navigate(callback(value) + '&id_token=forbidden'); await owner.retire(value); }
    await tick();
    const state = await owner.read(value);
    assert.equal(state.notification.notification, 'unavailable'); assert.equal(h.flows.length, 1);
    assert.equal(h.alarms.get('browser-auth-expiry').when, value.task.expires_at * 1000);
    assert.equal(JSON.stringify(h.stored).includes('Sensitive'), false);
    assert.equal(JSON.stringify(h.stored).includes('id_token'), false);
    await owner.launch(value, async () => {}); assert.equal(h.flows.length, 2);
    if (mode === 'shutdown') {
      h.flows[0].close(); await tick();
      assert.equal((await owner.read(value)).state, 'opening');
    }
  });
}

test('window closure is a durable local observation and never reopens the same child', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {});
  h.flows[0].close(); await tick(); await tick();
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
  h.chromeApi.windows.create = () => { throw Error('The user did not approve access.'); };
  await h.owner.launch(value, async () => {}); await tick(); await tick();
  assert.equal((await h.owner.read(value)).state, 'checking');
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'unknown');
});

test('only admitted preparation recovery with a fresh generation reopens a closed child once', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {});
  h.flows[0].close(); await tick(); await tick();
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
  assert.equal(h.flows[1].options.url, value.ceremony.entry_url + '#system-browser');
  h.flows[1].close(); await tick(); await tick();
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
  await assert.rejects(h.owner.retire(replacement), /task_changed/u);
  assert.equal((await h.owner.read(value)).state, 'opening');
  const reissued = structuredClone(value); reissued.task.admission_generation = id(20);
  await h.owner.retire(reissued); await tick();
  assert.equal((await h.owner.read(value)).notification.notification, 'unavailable');
  assert.deepEqual(h.removed, [h.flows[0].windowId]);
});

test('successor authentication tasks share their parent ceremony without sharing outcomes', async t => {
  const h = harness(t), first = h.request(), successor = structuredClone(first);
  successor.task.task_id = id(2); successor.task.operation_id = id(52);
  successor.task.scope_digest = 'b'.repeat(64); successor.task.source_revision++;
  await h.owner.launch(first, async () => {});
  await h.owner.retire(first); await tick(); await tick();
  assert.equal((await h.owner.read(first)).notification.notification, 'unavailable');
  assert.deepEqual(await h.owner.read(successor), { state: 'idle', notification: null });
  await h.owner.launch(successor, async () => {});
  assert.equal(h.flows.length, 2);
  assert.equal((await h.owner.read(successor)).state, 'opening');
  h.flows[1].close(); await tick(); await tick();
  assert.deepEqual(await h.owner.read(successor), { state: 'closed', notification: null });
  assert.equal((await h.owner.read(first)).notification.notification, 'unavailable');
  const restarted = h.make();
  assert.deepEqual(await restarted.read(successor), { state: 'closed', notification: null });
  assert.equal((await restarted.read(first)).notification.notification, 'unavailable');
  assert.equal(h.stored[BROWSER_AUTH_KEY].length, 2);
});

test('ending one task changes only its exact record while its successor remains active', async t => {
  const h = harness(t), first = h.request(), successor = structuredClone(first);
  successor.task.task_id = id(2); successor.task.operation_id = id(52);
  await h.owner.launch(first, async () => {});
  await h.owner.launch(successor, async () => {});
  await h.owner.retire(first); await tick(); await tick();
  assert.equal((await h.owner.read(first)).notification.notification, 'unavailable');
  assert.equal((await h.owner.read(successor)).state, 'opening');
  assert.deepEqual(h.removed, [h.flows[0].windowId]);
  assert.equal(h.owner.hasLiveFlow(), true);
  h.flows[1].close(); await tick(); await tick();
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

test('fixed expiry discards late closures and bounds retained correlations without evicting live work', async t => {
  const h = harness(t), requests = Array.from({ length: 9 }, (_, index) => h.request(index + 1));
  for (const value of requests.slice(0, 8)) await h.owner.launch(value, async () => {});
  await assert.rejects(h.owner.launch(requests[8], async () => {}), /authentication_unavailable/u);
  assert.equal(h.flows.length, 8); assert.equal(h.stored[BROWSER_AUTH_KEY].length, 8);
  h.advance(120001); await h.alarm();
  h.flows[0].close(); await tick();
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
  await h.owner.retire(value); await tick(); await tick();
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

const settle = async () => { for (let index = 0; index < 4; index++) await tick(); };

test('the popup opens on the ceremony entry in system-browser presentation', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {}); await settle();
  assert.equal(h.flows.length, 1);
  assert.equal(h.flows[0].options.url, value.ceremony.entry_url + '#system-browser');
  assert.equal(h.flows[0].options.type, 'popup');
  assertBoundedJournal(h);
});

test('no tab URL is watched and the old return URI changes nothing', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {}); await settle();
  assert.equal(h.onUpdated.listeners.size, 0);
  const flow = h.flows[0];
  for (const url of [callback(value), callback(value, 'retired'), callback(value) + '&id_token=forbidden',
    `${RETURN}?notification=changed`, 'https://evil.example/?next=' + encodeURIComponent(callback(value))]) {
    flow.navigate(url);
    h.onUpdated.emit(flow.tabId, { url }, { id: flow.tabId, windowId: flow.windowId });
  }
  await settle();
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'opening');
  assert.equal((await h.owner.read(value)).state, 'opening');
  assert.deepEqual(h.removed, []); assert.equal(h.owner.hasLiveFlow(), true);
  assert.equal(h.onRemoved.listeners.size, 1);
  assertBoundedJournal(h);
});

test('closing the popup while it is opening records closure with its admission generation', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {});
  h.onRemoved.emit(h.flows[0].windowId + 50); await settle();
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'opening');
  h.flows[0].close(); await settle();
  const record = h.stored[BROWSER_AUTH_KEY][0];
  assert.equal(record.phase, 'closed'); assert.equal(record.closed_generation, value.task.admission_generation);
  assert.deepEqual(h.removed, []);
  assert.equal(h.onUpdated.listeners.size, 0); assert.equal(h.onRemoved.listeners.size, 0);
  assert.deepEqual(await h.owner.read(value), { state: 'closed', notification: null });
  assertBoundedJournal(h);
});

test('the end of the browser task removes the popup without recording closure', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {}); await settle();
  await h.owner.retire(value); await settle();
  assert.deepEqual(h.removed, [h.flows[0].windowId]);
  const record = h.stored[BROWSER_AUTH_KEY][0];
  assert.equal(record.phase, 'unknown'); assert.equal(record.notification, null);
  assert.equal('closed_generation' in record, false);
  assert.deepEqual(await h.owner.read(value), { state: 'checking', notification: {
    ceremony_id: value.ceremony.ceremony_id, attempt_id: value.ceremony.attempt_id, notification: 'unavailable' } });
  assert.equal(h.owner.hasLiveFlow(), false);
  assert.equal(h.onRemoved.listeners.size, 0);
  await h.owner.retire(value); await settle();
  assert.deepEqual(h.removed, [h.flows[0].windowId], 'a settled attempt removes nothing');
  assertBoundedJournal(h);
});

test('expiry removes the popup without recording closure', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {}); await settle();
  h.advance(120001); await h.alarm(); await settle();
  assert.deepEqual(h.removed, [h.flows[0].windowId]);
  assert.deepEqual(h.stored[BROWSER_AUTH_KEY], []);
  assert.equal(h.owner.hasLiveFlow(), false);
  assert.equal(h.onRemoved.listeners.size, 0);
  await assert.rejects(h.owner.read(value), /task_expired/u);
});

test('a popup created after its task ended is removed as soon as it exists', async t => {
  const h = harness(t), value = h.request(), gate = deferred();
  h.control.createGate = gate.promise;
  await h.owner.launch(value, async () => {}); await settle();
  await h.owner.retire(value); await settle();
  assert.deepEqual(h.removed, []);
  gate.resolve(); await settle();
  assert.deepEqual(h.removed, [h.flows[0].windowId]);
  assert.equal(h.stored[BROWSER_AUTH_KEY][0].phase, 'unknown');
  assert.equal('closed_generation' in h.stored[BROWSER_AUTH_KEY][0], false);
});

test('a popup that cannot be created is an unknown outcome and never closure', async t => {
  const h = harness(t), value = h.request();
  h.control.createFailure = Error('The user did not approve access.');
  await h.owner.launch(value, async () => {}); await settle();
  assert.equal(h.flows.length, 1);
  const record = h.stored[BROWSER_AUTH_KEY][0];
  assert.equal(record.phase, 'unknown'); assert.equal('closed_generation' in record, false);
  assert.equal((await h.owner.read(value)).notification.notification, 'unavailable');
  assert.equal(h.owner.hasLiveFlow(), false);
  assert.equal(h.onUpdated.listeners.size, 0); assert.equal(h.onRemoved.listeners.size, 0);
  assertBoundedJournal(h);
});

test('a second action for a live popup focuses it instead of opening another', async t => {
  const h = harness(t), value = h.request();
  await h.owner.launch(value, async () => {}); await settle();
  assert.equal((await h.owner.launch(value, async () => {})).state, 'opening');
  assert.equal(h.flows.length, 1);
  assert.deepEqual(h.focused, [[h.flows[0].windowId, { focused: true }]]);
  h.chromeApi.windows.update = async () => { throw Error('No window'); };
  assert.equal((await h.owner.launch(value, async () => {})).state, 'opening');
  assert.equal(h.flows.length, 1);
  assert.equal(h.onUpdated.listeners.size, 0); assert.equal(h.onRemoved.listeners.size, 1);
  assertBoundedJournal(h);
});

test('the built manifest adds no host permission and no tab-wide permission', async () => {
  const extensionRoot = fileURLToPath(new URL('..', import.meta.url));
  const release = fileURLToPath(new URL('./fixtures/customer-release.synthetic.json', import.meta.url));
  const source = JSON.parse(readFileSync(path.join(extensionRoot, 'manifest.json'), 'utf8'));
  const outdir = await mkdtemp(path.join(tmpdir(), 'ofca-auth-manifest-'));
  try {
    await promisify(execFile)(process.execPath, [path.join(extensionRoot, 'build.mjs'),
      `--customer-release-config=${release}`, `--outdir=${outdir}`], { cwd: extensionRoot });
    const manifest = JSON.parse(await readFile(path.join(outdir, 'manifest.json'), 'utf8'));
    const hosted = JSON.parse(readFileSync(release, 'utf8')).hosted_api_origin;
    assert.deepEqual(manifest.host_permissions, source.host_permissions);
    assert.deepEqual(manifest.host_permissions, ['http://bridge.localhost/*']);
    assert.equal(JSON.stringify(manifest).includes(hosted), false);
    assert.equal(JSON.stringify(manifest).includes('chromiumapp'), false);
    const declared = [...manifest.permissions, ...(manifest.optional_permissions ?? [])];
    for (const permission of ['tabs', 'webNavigation', 'webRequestBlocking']) assert.equal(declared.includes(permission), false, permission);
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
});
