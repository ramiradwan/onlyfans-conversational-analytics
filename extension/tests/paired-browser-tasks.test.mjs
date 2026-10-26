import test from 'node:test';
import assert from 'node:assert/strict';
import { createPairedBrowserTasks } from '../runtime/paired-browser-tasks.mjs';
import { createBrowserAuth } from '../runtime/browser-auth.mjs';
import { createAuthenticationTaskSession } from '../runtime/authentication-task-session.mjs';
import { payload, observation } from '../test-fixtures/browser-task.mjs';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function harness(t) {
  let time = Date.now();
  const value = payload(time), stored = {}, flows = [], notifications = [], abort = new AbortController();
  value.task.purpose = 'browser.authentication'; value.creator_account_id = 'creator-test';
  value.ceremony = { entry_url: 'https://provider.example/onboarding/browser-ceremonies/' + 'A'.repeat(43),
    ceremony_id: '0199b234-5678-7000-8000-000000000001', attempt_id: 'A'.repeat(43),
    completion: { mode: 'extension-auth-flow', return_uri: 'https://' + value.extension_id + '.chromiumapp.org/browser-ceremony/v1/complete' },
    expires_at: new Date(value.task.expires_at * 1000).toISOString() };
  const chromeApi = { runtime: { id: value.extension_id }, storage: { local: {
    async get(key) { return structuredClone({ [key]: stored[key] }); }, async set(change) { Object.assign(stored, structuredClone(change)); },
  } }, alarms: { async create() {}, async clear() {}, onAlarm: { addListener() {}, removeListener() {} } },
  identity: { getRedirectURL: path => 'https://' + value.extension_id + '.chromiumapp.org/' + path,
    launchWebAuthFlow(options) { return new Promise((resolve, reject) => flows.push({ options, resolve, reject })); } } };
  const authentication = createBrowserAuth({ chromeApi, hostedOrigin: 'https://provider.example', now: () => time });
  let reads = 0, afterRead = () => {}, afterAttach = () => {};
  const leases = new Set();
  const state = { ...observation(value.task), state: 'paused', creator_account_id: value.creator_account_id };
  const owner = { async attach() { const lease = {}; leases.add(lease); await afterAttach(); return lease; },
    async release(lease) { assert.equal(leases.delete(lease), true, 'each attachment releases once'); },
    async readPaired(_context, account) {
    assert.equal(account, value.creator_account_id); reads++; afterRead(reads); return { ...state };
  } };
  const tasks = createPairedBrowserTasks({ owner, authentication, now: () => time });
  const controls = { creatorAccountId: value.creator_account_id, signal: abort.signal,
    guard() { if (abort.signal.aborted) throw Error('session_closed'); }, async notify(value) { notifications.push(value); } };
  t.after(() => { abort.abort(); tasks.dispose(); authentication.dispose(); });
  return { value, tasks, controls, flows, notifications, state, abort, authentication, leases,
    advance(ms) { time += ms; }, afterRead(fn) { afterRead = fn; }, afterAttach(fn) { afterAttach = fn; } };
}

test('paired dispatch reuses its exact flow and pushes only bounded outcome correlation', async t => {
  const h = harness(t);
  assert.equal(h.flows.length, 0);
  await h.tasks.run(h.value, h.controls);
  await h.tasks.run(h.value, h.controls);
  assert.equal(h.flows.length, 1);
  assert.equal(h.notifications.at(-1).state, 'opening');
  let timer;
  t.after(() => clearTimeout(timer));
  const notified = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(Error('notification_timeout')), 5000);
    const notify = h.controls.notify;
    h.controls.notify = async value => {
      await notify(value);
      clearTimeout(timer);
      resolve();
    };
  });
  h.flows[0].resolve(h.value.ceremony.completion.return_uri + '?ceremony_id=' + h.value.ceremony.ceremony_id
    + '&attempt_id=' + h.value.ceremony.attempt_id + '&notification=changed');
  await notified;
  assert.deepEqual(h.notifications.at(-1), { state: 'checking', notification: {
    ceremony_id: h.value.ceremony.ceremony_id, attempt_id: h.value.ceremony.attempt_id, notification: 'changed' } });
  await h.tasks.run(h.value, h.controls); assert.equal(h.flows.length, 1);
});

test('popup closure sends the same bounded local withdrawal on paired redispatch without reopening', async t => {
  const h = harness(t);
  await h.tasks.run(h.value, h.controls);
  let timer;
  t.after(() => clearTimeout(timer));
  const closed = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(Error('notification_timeout')), 5000);
    const notify = h.controls.notify;
    h.controls.notify = async value => {
      await notify(value);
      if (value.state === 'closed') { clearTimeout(timer); resolve(); }
    };
  });
  h.flows[0].reject(Error('The user did not approve access.'));
  await closed;
  assert.deepEqual(h.notifications.at(-1), { state: 'closed', notification: null });
  await h.tasks.run(h.value, h.controls);
  assert.equal(h.flows.length, 1);
  assert.deepEqual(h.notifications.at(-1), { state: 'closed', notification: null });
});

test('paused dispatch still checks current consent and cannot open after revocation', async t => {
  const h = harness(t);
  h.afterRead(reads => { if (reads === 2) Object.assign(h.state, { state: 'needs-consent', creator_account_id: null }); });
  await h.tasks.run(h.value, h.controls);
  assert.equal(h.flows.length, 0);
  assert.equal(h.notifications.at(-1).notification.notification, 'unavailable');
});

test('closed channels, wrong accounts, other purposes and expired tasks never open', async t => {
  const h = harness(t);
  await assert.rejects(h.tasks.run({ ...h.value, creator_account_id: 'another' }, h.controls), /task_changed/u);
  await assert.rejects(h.tasks.run({ ...h.value, task: { ...h.value.task, purpose: 'browser.access' } }, h.controls), /task_changed/u);
  h.abort.abort();
  await assert.rejects(h.tasks.run(h.value, h.controls), /session_closed/u);
  h.advance(31000);
  await assert.rejects(h.tasks.run(h.value, h.controls), /task_expired/u);
  assert.equal(h.flows.length, 0);
});

test('lost channel suppresses late notifications without claiming the browser failed', async t => {
  const h = harness(t); await h.tasks.run(h.value, h.controls);
  const count = h.notifications.length;
  assert.equal(h.leases.size, 1); assert.equal(h.authentication.hasLiveFlow(), true);
  h.abort.abort(); h.flows[0].reject(Error('untrusted browser text'));
  assert.equal(h.leases.size, 0); assert.equal(h.authentication.hasLiveFlow(), true);
  await tick(); await tick();
  assert.equal(h.notifications.length, count);
  assert.equal((await h.authentication.read(h.value)).notification.notification, 'unavailable');
  assert.equal(h.authentication.hasLiveFlow(), false);
});

test('expiry while attaching releases the original lease and cannot launch', async t => {
  const h = harness(t);
  h.afterAttach(() => h.advance(31000));
  await h.tasks.run(h.value, h.controls);
  assert.equal(h.flows.length, 0);
  assert.equal(h.leases.size, 0);
  assert.equal(h.notifications.at(-1).notification.notification, 'unavailable');
});

test('shared auth lifecycle suppresses a read that finishes after its admission closes', async () => {
  const abort = new AbortController(), context = {}, sent = [], released = [], listeners = new Set();
  let resolve, entered;
  const reading = new Promise(done => { entered = done; });
  const authentication = { subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    read() { entered(); return new Promise(done => { resolve = done; }); },
    launch() { assert.fail('A lifecycle read cannot launch authentication'); } };
  const session = createAuthenticationTaskSession({ owner: { async release(value) { released.push(value); } },
    context, authentication, payload: {}, signal: abort.signal, deadline: Date.now() + 10000,
    authorize() { if (abort.signal.aborted) throw Error('task_changed'); }, publish: value => sent.push(value) });
  const read = session.refresh(); await reading;
  abort.abort(); session.close();
  resolve({ state: 'checking' }); await read;
  assert.deepEqual(released, [context]); assert.deepEqual(sent, []); assert.equal(listeners.size, 0);
});

test('shared auth lifecycle rechecks authority and retires only its exact subscription and lease', async () => {
  const listeners = new Set(), released = [], sent = [], abort = new AbortController();
  let allowed = true;
  const authentication = { subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    async read() { allowed = false; return { state: 'checking' }; } };
  const original = {}, successor = {};
  const create = context => createAuthenticationTaskSession({ owner: { async release(value) { released.push(value); } },
    context, authentication, payload: {}, signal: abort.signal, deadline: Date.now() + 10000,
    authorize() { if (!allowed) throw Error('task_changed'); }, publish: value => sent.push(value) });
  const first = create(original), second = create(successor);
  try {
    await assert.rejects(first.refresh(), /task_changed/u);
    assert.deepEqual(sent, []);
    first.close(); first.close();
    assert.equal(listeners.size, 1); assert.deepEqual(released, [original]);
    allowed = true; authentication.read = async () => ({ state: 'checking' });
    await second.refresh(); await second.refresh();
    assert.equal(sent.length, 1);
    abort.abort(); assert.deepEqual(released, [original, successor]); assert.equal(listeners.size, 0);
  } finally { first.close(); second.close(); }
});
