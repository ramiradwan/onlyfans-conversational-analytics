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
    emit: (...args) => { for (const fn of listeners) fn(...args); } };
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
        classList: { toggle(name, value) { if (name === 'hidden') nodes.get(id).hidden = value; } },
        setAttribute() {}, removeAttribute() {}, getClientRects: () => [], scrollIntoView() {}, querySelector: () => null,
        addEventListener: (type, fn) => listeners.set(type, fn), dispatch: (type) => listeners.get(type)?.() });
    }
    return nodes.get(id);
  };
  const model = { status: { consent: { mode, resume_mode: mode === 'paused' ? 'full' : null }, phase: mode },
    legal: { configured: true, flow: { stage: 'pre_mode', transaction_id: 'fixture-review', terms_event_id: null, risk_event_id: null } } };
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
    URL, TextEncoder, AbortController, setTimeout, clearTimeout, requestAnimationFrame: setImmediate, HTMLElement: class {},
    location: { hash: '' }, sessionStorage: { getItem() { return null; } },
    window: { addEventListener() {}, removeEventListener() {} },
    document: { getElementById: node, querySelector: node, activeElement: null, body: null,
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

for (const [id, command, field] of [
  ['terms-accepted', 'accept-terms', 'terms_event_id'],
  ['risk-acknowledged', 'acknowledge-risk', 'risk_event_id'],
]) test(`${id} writes only its own action and recovers a lost response`, async (t) => {
  const h = await surface(t, 'setup'); const messages = [];
  await h.node('start-preview').dispatch('click');
  await h.client.sync(); await tick();
  h.setAction((message) => {
    messages.push(message);
    h.model.legal.flow[field] = 'fixture-receipt';
    throw Error('fixture_lost_response');
  });
  h.node(id).checked = true;
  await h.node(id).dispatch('change');
  assert.equal(h.page.busy, false);
  assert.equal(h.node(id).checked, true);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].type, `ofca.legal-activation.${command}`);
  assert.equal(messages[0].transactionId, 'fixture-review');
  assert.equal(h.node('enable-preview').disabled, true);
});

test('paused Full remains a desktop handoff as control sessions change', async (t) => {
  const h = await surface(t, 'popup', 'paused');
  assert.equal(h.node('journey-primary').textContent, 'Continue in the desktop app');
  h.setControl(true);
  h.port.onMessage.emit({ type: 'surface_changed' });
  await h.client.refresh();
  await tick();
  assert.equal(h.node('journey-primary').textContent, 'Continue in the desktop app');
  h.setControl(false);
  h.port.onMessage.emit({ type: 'surface_changed' });
  await h.client.refresh();
  await tick();
  assert.equal(h.node('journey-primary').textContent, 'Continue in the desktop app');
});

test('setup reconnecting hides desktop actions and the setup-code hint', async (t) => {
  const h = await surface(t, 'setup', 'full');
  h.port.onDisconnect.emit();
  assert.equal(h.node('desktop-entry-title').textContent, 'Reconnecting to the extension');
  assert.equal(h.node('desktop-icon-mark').dataset.icon, 'pending');
  for (const id of ['open-desktop', 'download-desktop', 'desktop-setup-code']) {
    assert.equal(h.node(id).hidden, true, id);
  }
});

test('setup and popup omit #retry-runtime and recover passively through surface-client', async (t) => {
  for (const page of ['setup', 'popup']) {
    const markup = await readFile(new URL(`../${page}.html`, import.meta.url), 'utf8');
    assert.doesNotMatch(markup, /id="retry-runtime"/);
    const section = markup.match(/<section id="runtime-unavailable"[\s\S]*?<\/section>/)?.[0] ?? '';
    assert.ok(section.includes('id="runtime-body"'), `${page}.html must include #runtime-body`);
    assert.doesNotMatch(section, /Try again/i);
    assert.ok(section.includes(page === 'setup' ? 'Dipsy can’t connect right now.' : "Dipsy can't connect right now."));

    const source = new URL(`../${page}.js`, import.meta.url);
    const bundle = await build({
      stdin: {
        contents: `${await readFile(source, 'utf8')}\nglobalThis.probe = {client};`,
        resolveDir: fileURLToPath(new URL('../', import.meta.url)),
        sourcefile: `${page}.js`,
      },
      bundle: true, write: false, format: 'iife',
    });
    const nodes = new Map();
    const queried = new Set();
    const node = (id) => {
      queried.add(id);
      if (!nodes.has(id)) {
        const classes = new Set();
        nodes.set(id, {
          dataset: {}, disabled: false, checked: false, textContent: '',
          classList: {
            toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
            contains(name) { return classes.has(name); },
          },
          setAttribute() {}, removeAttribute() {},
          addEventListener() {}, querySelector: () => null,
        });
      }
      return nodes.get(id);
    };
    let runtimeDown = true, connectAllowed = false;
    const windowListeners = new Map();
    const storageChanged = event();
    const port = { onMessage: event(), onDisconnect: event(), disconnect() {}, postMessage() {} };
    const sandbox = {
      chrome: {
        runtime: {
          getURL: () => 'fixture',
          connect() {
            if (!connectAllowed) throw new Error('worker_unavailable');
            return port;
          },
          async sendMessage(message) {
            if (runtimeDown) throw new Error('worker_unavailable');
            if (message.type === 'ofca.ui.status') return { ok: true, status: { consent: { mode: 'off', resume_mode: null }, phase: 'off' } };
            if (message.type === 'ofca.legal-activation.status') {
              return { ok: true, result: { configured: true, flow: { stage: 'pre_mode', terms_event_id: null, risk_event_id: null } } };
            }
            return { ok: true, result: null };
          },
        },
        storage: { onChanged: storageChanged, session: { async get() { return {}; } } },
      },
      URL, TextEncoder, AbortController, setTimeout, clearTimeout, HTMLElement: class {},
      location: { hash: '' }, sessionStorage: { getItem() { return null; } },
      window: {
        addEventListener(name, fn) { windowListeners.set(name, fn); },
        removeEventListener() {},
      },
      document: {
        visibilityState: 'visible',
        getElementById: node, querySelector: node, querySelectorAll: () => [],
        addEventListener() {}, removeEventListener() {},
      },
      fetch: async () => ({ json: async () => ({}) }),
    };
    runInNewContext(bundle.outputFiles[0].text, sandbox);
    t.after(() => sandbox.probe.client.stop());
    for (let i = 0; i < 30 && node('main').dataset.initialized !== 'true'; i++) await tick();
    assert.equal(node('main').dataset.initialized, 'true');
    assert.equal(queried.has('retry-runtime'), false, `${page}.js must not bind #retry-runtime`);
    assert.equal(node('runtime-unavailable').classList.contains('hidden'), false);
    assert.equal(node('runtime-body').textContent, 'Connection interrupted. Reconnecting…');
    runtimeDown = false; connectAllowed = true;
    storageChanged.emit({ recovery: { newValue: true } }, 'local');
    for (let i = 0; i < 30 && node('main').dataset.ready !== 'true'; i++) await tick();
    assert.equal(node('runtime-unavailable').classList.contains('hidden'), true);
    assert.equal(node('main').dataset.ready, 'true');
  }
});
