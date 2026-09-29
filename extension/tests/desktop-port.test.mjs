import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DESKTOP_LINK_STORAGE_KEY,
  DESKTOP_PORT_NAME,
  admitDesktopSender,
  desktopStage,
  isDesktopMessage,
  registerDesktopPort,
} from '../runtime/desktop-port.mjs';

const ORIGIN = 'http://bridge.localhost:17871';
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const event = () => {
  const listeners = [];
  return { listeners, addListener(fn) { listeners.push(fn); }, removeListener(fn) { listeners.splice(listeners.indexOf(fn), 1); } };
};
const bridgeSender = (extra = {}) => ({
  origin: ORIGIN, url: `${ORIGIN}/settings`, frameId: 0, tab: { id: 12, windowId: 4 }, ...extra,
});
const legal = (overrides = {}) => ({
  configured: true, requires_reauthorization: false,
  flow: { terms_event_id: 'terms', risk_event_id: 'risk', stage: 'mode_selection' }, ...overrides,
});
const consent = (mode, extra = {}) => ({ consent: { mode }, phase: mode === 'full' ? 'identity' : mode, reload_required: false, ...extra });

test('only the top-level desktop app page in a browser tab is admitted', () => {
  assert.equal(admitDesktopSender(bridgeSender(), ORIGIN), true);
  assert.equal(admitDesktopSender({ ...bridgeSender(), origin: undefined }, ORIGIN), true);
  for (const sender of [
    bridgeSender({ origin: 'http://127.0.0.1:17871', url: 'http://127.0.0.1:17871/' }),
    bridgeSender({ origin: 'https://bridge.localhost:17871' }),
    bridgeSender({ frameId: 3 }),
    bridgeSender({ frameId: undefined }),
    bridgeSender({ tab: undefined }),
    bridgeSender({ tab: { id: -1, windowId: 1 } }),
    bridgeSender({ id: 'another-extension' }),
    {}, null,
  ]) assert.equal(admitDesktopSender(sender, ORIGIN), false, JSON.stringify(sender));
});

test('the stage follows the extension-owned prerequisites in order', () => {
  const stage = (value) => desktopStage(value);
  assert.equal(stage({ consent: consent('off'), legal: { configured: false } }), 'unavailable');
  assert.equal(stage({ consent: consent('off'), legal: legal({ flow: { stage: 'pre_mode' } }) }), 'needs_terms');
  assert.equal(stage({ consent: consent('full'), legal: legal({ requires_reauthorization: true }) }), 'needs_terms');
  assert.equal(stage({ consent: consent('off'), legal: legal() }), 'needs_full');
  assert.equal(stage({ consent: consent('preview'), legal: legal() }), 'needs_full');
  assert.equal(stage({ consent: consent('paused'), legal: legal() }), 'paused');
  assert.equal(stage({ consent: consent('full', { phase: 'permission_required' }), legal: legal() }), 'needs_site_access');
  assert.equal(stage({ consent: consent('full', { reload_required: true }), legal: legal() }), 'needs_site_access');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'setup_incomplete' } }), 'needs_account');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'unpaired' } }), 'ready_to_pair');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'pairing_failed' } }), 'ready_to_pair');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'compare' } }), 'pairing');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'paired' } }), 'paired');
});

test('port messages have closed schemas', () => {
  assert.equal(isDesktopMessage({ type: 'open', version: 1, step: 'setup' }), true);
  assert.equal(isDesktopMessage({ type: 'open', version: 1, step: 'connection' }), true);
  assert.equal(isDesktopMessage({ type: 'pair', version: 1 }), true);
  assert.equal(isDesktopMessage({ type: 'cancel', version: 1 }), true);
  for (const message of [
    { type: 'open', version: 1, step: 'data' }, { type: 'open', version: 1 }, { type: 'pair' },
    { type: 'pair', version: 2 }, { type: 'pair', version: 1, extra: true }, { type: 'confirm', version: 1 },
    { type: 'forget', version: 1 }, null, [],
  ]) assert.equal(isDesktopMessage(message), false, JSON.stringify(message));
});

function harness({ state = { consent: consent('full'), legal: legal(), pairing: { state: 'unpaired' } } } = {}) {
  const onConnectExternal = event();
  const session = {};
  const chromeApi = { runtime: { onConnectExternal }, storage: { session: { async set(value) { Object.assign(session, value); } } } };
  const subscribers = new Set();
  let owner = null, pairing = null;
  const calls = { pair: 0, cancel: 0, open: [], reads: 0 };
  const companion = {
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
    owns(value) { return owner !== null && owner === value; },
    cancelFor(value) { if (owner === value) { calls.cancel += 1; pairing?.reject(new Error('cancelled')); } },
    async status() { return state.pairing; },
    pairFor(value, { signal }) {
      if (owner !== null) return Promise.resolve(false);
      owner = value; calls.pair += 1;
      return new Promise((resolve, reject) => {
        pairing = { resolve: () => { owner = null; resolve(true); }, reject: (error) => { owner = null; reject(error); } };
        signal.addEventListener('abort', () => { calls.cancel += 1; pairing.reject(new Error('aborted')); }, { once: true });
      });
    },
  };
  const announce = (value) => { for (const fn of subscribers) fn(value); };
  let clock = 0;
  const desktop = registerDesktopPort({
    chromeApi, companion,
    readState: async () => { calls.reads += 1; return state; },
    openStep: async (step, options) => { calls.open.push({ step, ...options }); },
    now: () => clock,
  });
  desktop.register();
  const connect = (sender = bridgeSender()) => {
    const port = { name: DESKTOP_PORT_NAME, sender, sent: [], disconnected: false, onMessage: event(), onDisconnect: event(),
      postMessage(message) { this.sent.push(message); }, disconnect() { this.disconnected = true; } };
    onConnectExternal.listeners[0](port);
    return port;
  };
  return { desktop, connect, calls, session, announce, state, advance: (ms) => { clock += ms; },
    resolvePairing: () => pairing.resolve(), setState: (next) => Object.assign(state, next) };
}

test('a refused sender is disconnected without reading state', async () => {
  const h = harness();
  const port = h.connect(bridgeSender({ frameId: 1 }));
  await tick();
  assert.equal(port.disconnected, true);
  assert.deepEqual(port.sent, []);
  assert.equal(h.calls.reads, 0);
});

test('state is pushed on connect and again only when it changes', async () => {
  const h = harness();
  const port = h.connect();
  await tick();
  assert.deepEqual(port.sent, [{ type: 'state', version: 1, stage: 'ready_to_pair', attempt: null }]);
  assert.equal(h.session[DESKTOP_LINK_STORAGE_KEY], 1);
  h.desktop.changed(); await tick();
  assert.equal(port.sent.length, 1, 'an unchanged stage is not re-sent');
  h.setState({ pairing: { state: 'paired' } });
  h.desktop.changed(); await tick();
  assert.equal(port.sent.at(-1).stage, 'paired');
  port.onDisconnect.listeners[0]();
  await tick();
  assert.equal(h.session[DESKTOP_LINK_STORAGE_KEY], 0);
});

test('bursts of change events coalesce into one state read', async () => {
  const h = harness();
  h.connect(); await tick();
  const before = h.calls.reads;
  for (let index = 0; index < 5; index += 1) h.desktop.changed();
  await tick();
  assert.equal(h.calls.reads, before + 1);
});

test('only the owning port sees its comparison code, and closing it cancels the attempt', async () => {
  const h = harness();
  const owner = h.connect(), observer = h.connect(bridgeSender({ tab: { id: 13, windowId: 4 } }));
  await tick();
  owner.onMessage.listeners[0]({ type: 'pair', version: 1 });
  observer.onMessage.listeners[0]({ type: 'pair', version: 1 });
  await tick();
  assert.equal(h.calls.pair, 1, 'a second port cannot take over the attempt');
  h.setState({ pairing: { state: 'compare' } });
  h.announce({ state: 'compare', comparison_code: '042917' });
  await tick();
  assert.deepEqual(owner.sent.at(-1).attempt, { state: 'compare', comparison_code: '042917' });
  assert.equal(owner.sent.at(-1).stage, 'pairing');
  assert.ok(observer.sent.every((message) => message.attempt === null));
  owner.onDisconnect.listeners[0]();
  await tick();
  assert.equal(h.calls.cancel, 1);
});

test('a completed attempt is reported to its owner', async () => {
  const h = harness();
  const port = h.connect(); await tick();
  port.onMessage.listeners[0]({ type: 'pair', version: 1 });
  await tick();
  h.setState({ pairing: { state: 'paired' } });
  h.resolvePairing();
  await tick(); await tick();
  assert.deepEqual(port.sent.at(-1), { type: 'state', version: 1, stage: 'paired', attempt: { state: 'paired', comparison_code: null } });
});

test('opening a page is rate-limited and anchored to the desktop tab', async () => {
  const h = harness();
  const port = h.connect(); await tick();
  port.onMessage.listeners[0]({ type: 'open', version: 1, step: 'setup' });
  port.onMessage.listeners[0]({ type: 'open', version: 1, step: 'setup' });
  h.advance(1_000);
  port.onMessage.listeners[0]({ type: 'open', version: 1, step: 'connection' });
  port.onMessage.listeners[0]({ type: 'open', version: 1, step: 'https://example.test' });
  await tick();
  assert.deepEqual(h.calls.open, [
    { step: 'setup', anchorTab: { id: 12, windowId: 4 } },
    { step: 'connection', anchorTab: { id: 12, windowId: 4 } },
  ]);
  assert.equal(h.calls.pair, 0);
});
