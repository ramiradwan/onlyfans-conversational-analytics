import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';

const tick = () => new Promise(setImmediate);
function event() {
  const listeners = new Set();
  return { addListener: (fn) => listeners.add(fn), removeListener: (fn) => listeners.delete(fn),
    emit: (value) => { for (const fn of listeners) fn(value); } };
}
async function surface(t, entry, mode = 'off') {
  const source = new URL(`../${entry}.js`, import.meta.url);
  const bundle = await build({ stdin: { contents: `${await readFile(source, 'utf8')}\nglobalThis.probe = {page, client};`,
    resolveDir: fileURLToPath(new URL('../', import.meta.url)), sourcefile: `${entry}.js` },
  bundle: true, write: false, format: 'iife' });
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) {
      const listeners = new Map();
      nodes.set(id, { dataset: {}, disabled: false, checked: false, textContent: '',
        classList: { toggle() {} }, setAttribute() {}, removeAttribute() {},
        addEventListener: (type, fn) => listeners.set(type, fn), dispatch: (type) => listeners.get(type)?.() });
    }
    return nodes.get(id);
  };
  const model = { status: { consent: { mode, resume_mode: mode === 'paused' ? 'full' : null }, phase: mode },
    legal: { configured: true, flow: { stage: 'pre_mode', terms_event_id: null, risk_event_id: null } } };
  let action = null, desktopControl = false, releaseStatus = null;
  const port = { onMessage: event(), onDisconnect: event(), disconnect() {}, postMessage(message) {
    if (message.type === 'status') queueMicrotask(() => port.onMessage.emit({ state: 'unavailable', desktop_control: desktopControl }));
  } };
  const sandbox = {
    chrome: { runtime: { getURL: () => 'fixture', connect: () => port, async sendMessage(message) {
      if (message.type === 'ofca.ui.status') {
        const status = structuredClone(model.status);
        if (releaseStatus) await releaseStatus.promise;
        return { ok: true, status };
      }
      if (message.type === 'ofca.legal-activation.status') return { ok: true, result: structuredClone(model.legal) };
      if (action) return action(message);
      throw new Error('unexpected_command');
    } }, storage: { onChanged: event(), session: { async get() { return {}; } } } },
    URL, TextEncoder, AbortController, setTimeout, clearTimeout, HTMLElement: class {},
    location: { hash: '' }, sessionStorage: { getItem() { return null; } },
    window: { addEventListener() {}, removeEventListener() {} },
    document: { getElementById: node, querySelector: node,
      querySelectorAll: (selector) => selector === 'button, input' ? [...nodes.values()] : [],
      addEventListener() {}, removeEventListener() {} },
    fetch: async () => ({ json: async () => ({}) }),
  };
  runInNewContext(bundle.outputFiles[0].text, sandbox);
  const { page, client } = sandbox.probe;
  t.after(() => client.stop());
  for (let i = 0; i < 30 && node('main').dataset.ready !== 'true'; i++) await tick();
  assert.equal(node('main').dataset.ready, 'true');
  return { node, model, port, page, client, setAction(fn) { action = fn; },
    setControl(value) { desktopControl = value; },
    holdRefresh() {
      let resolve;
      releaseStatus = { promise: new Promise((done) => { resolve = done; }) };
      return () => { resolve(); releaseStatus = null; };
    } };
}

for (const id of ['terms-accepted', 'risk-acknowledged']) test(`${id} is a draft until the explicit consent button`, async (t) => {
  const h = await surface(t, 'setup'); let commands = 0;
  h.setAction(() => { commands++; throw Error('unexpected legal acceptance'); });
  h.node(id).checked = true; h.node(id).dispatch('change');
  assert.equal(h.page.busy, false);
  h.port.onMessage.emit({ type: 'surface_changed' });
  await h.client.refresh(); await tick();
  assert.equal(h.node(id).checked, true, 'a status update preserves unfinished choices');
  assert.equal(h.node(id).disabled, false);
  assert.equal(commands, 0, 'checking a draft never records acceptance');
  assert.equal(h.node('activate-software').disabled, false, 'validation remains available');
});

test('a paused popup refreshes control ownership when a replacement session is admitted', async (t) => {
  const h = await surface(t, 'popup', 'paused');
  assert.equal(h.node('journey-primary').textContent, 'Resume analytics');
  h.setControl(true);
  h.port.onMessage.emit({ type: 'surface_changed' });
  await h.client.refresh();
  await tick();
  assert.equal(h.node('journey-primary').textContent, 'Resume in the desktop app');
  h.setControl(false);
  h.port.onMessage.emit({ type: 'surface_changed' });
  await h.client.refresh();
  await tick();
  assert.equal(h.node('journey-primary').textContent, 'Resume analytics');
});
