import assert from 'node:assert/strict';
import test from 'node:test';
import { BrowserTaskClient } from '../../frontend/browser-tasks/client.js';
import { connectExtension } from '../../frontend/browser-tasks/extension.js';
import { createBrowserAuth } from '../runtime/browser-auth.mjs';
import { DESKTOP_PORT_NAME, registerDesktopPort } from '../runtime/desktop-port.mjs';
import { payload, observation } from '../test-fixtures/browser-task.mjs';

const ORIGIN = 'https://provider.example';
const event = () => { const listeners = new Set(); return {
  addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
  emit: value => { for (const fn of listeners) fn(value); },
}; };
const area = () => { const values = {}; return {
  async get(key) { return key === undefined ? { ...values } : { [key]: values[key] }; },
  async set(update) { Object.assign(values, update); }, async remove(key) { delete values[key]; },
}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
async function until(predicate, limit = 5000) {
  const deadline = performance.now() + limit;
  while (!predicate() && performance.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  return predicate();
}

// Real helper, extension port and sign-in popup owner. Only Chrome and Brain's HTTP replies are simulated.
function harness(t, { preparingReads }) {
  const ready = payload();
  Object.assign(ready.task, { purpose: 'browser.authentication', state: 'pending', expires_at: Math.floor(Date.now() / 1000) + 1790 });
  ready.creator_account_id = 'synthetic-creator';
  ready.ceremony = { entry_url: `${ORIGIN}/onboarding/browser-ceremonies/${'A'.repeat(43)}`,
    ceremony_id: '0199b234-5678-7000-8000-000000000002', attempt_id: 'Q'.repeat(43),
    completion: { mode: 'extension-auth-flow', return_uri: `https://${ready.extension_id}.chromiumapp.org/browser-ceremony/v1/complete` },
    expires_at: new Date(Date.now() + 600000).toISOString() };
  const preparing = { ...ready, ceremony: null };
  const observed = { ...observation(ready.task), state: 'ready', creator_account_id: 'synthetic-creator' };
  const url = 'http://bridge.localhost:17871/browser-tasks#presentation=' + ready.task.admission_generation;
  const created = [];
  const chromeApi = {
    runtime: { id: ready.extension_id, onConnectExternal: event(), getURL: path => `chrome-extension://${ready.extension_id}/${path}` },
    identity: { getRedirectURL: path => `https://${ready.extension_id}.chromiumapp.org/${path}` },
    storage: { local: area(), session: area() },
    alarms: { onAlarm: event(), async create() {}, async clear() {} },
    windows: { onRemoved: event(), async create(options) { created.push(options); return { id: 40 + created.length, tabs: [{ id: 80 }] }; },
      async remove() {}, async update() {} },
    tabs: { async get() { return { url, windowId: 4 }; }, async update() {} },
    scripting: { async executeScript() { return [{ frameId: 0, documentId: 'document-1', result: true }]; } },
  };
  const authentication = createBrowserAuth({ chromeApi, hostedOrigin: ORIGIN });
  const owner = { async attach() { return {}; }, async read() { return { ...observed }; }, async release() {} };
  const companion = { subscribe: () => () => {}, owns: () => false, cancelFor() {} };
  const ports = registerDesktopPort({ chromeApi, owner, companion, authentication });
  ports.register();
  const runtime = { connect() {
    let extensionPort;
    const helperPort = { onMessage: event(), onDisconnect: event(), disconnect() { extensionPort.onDisconnect.emit(); },
      postMessage: message => extensionPort.onMessage.emit(message) };
    extensionPort = { name: DESKTOP_PORT_NAME, sender: { origin: 'http://bridge.localhost:17871', url, documentId: 'document-1', frameId: 0,
      tab: { id: 12, windowId: 4 } }, onMessage: event(), onDisconnect: event(), disconnect() { helperPort.onDisconnect.emit(); },
      postMessage: message => helperPort.onMessage.emit(message) };
    chromeApi.runtime.onConnectExternal.emit(extensionPort);
    return helperPort;
  } };
  let reads = 0;
  const presentations = [];
  const helper = new BrowserTaskClient({ generation: ready.task.admission_generation, nativeLaunch: true,
    receive: value => presentations.push(value.state), connect: options => connectExtension({ ...options, runtime }),
    events: async () => new Promise(() => {}),
    request: async path => {
      let response;
      if (path.endsWith('/session')) response = { role: 'browser_task', generation: ready.task.admission_generation,
        csrf_token: 'A'.repeat(43), task_id: ready.task.task_id, journey_id: ready.task.journey_id };
      else if (path.endsWith('/payload')) response = reads++ < preparingReads ? preparing : ready;
      else response = { state: 'recorded' };
      return { ok: true, text: async () => JSON.stringify(response) };
    } });
  t.after(async () => { helper.stop(); ports.unregister?.(); await tick(); });
  return { helper, created, presentations, ready, get reads() { return reads; } };
}

for (const preparingReads of [0, 3]) {
  test(`a sign-in helper opens its popup exactly once after ${preparingReads} preparing reads`, async t => {
    const h = harness(t, { preparingReads });
    await h.helper.start();
    assert.equal(await until(() => h.created.length > 0), true, `popup opened (presentations: ${h.presentations.join(',')})`);
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(h.created.length, 1);
    assert.equal(h.created[0].url, h.ready.ceremony.entry_url + '#system-browser');
    assert.ok(h.reads > preparingReads);
  });
}
