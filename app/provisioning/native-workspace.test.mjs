import assert from 'node:assert/strict';
import test from 'node:test';
import { createLocalWorkspaceOwner, returnToLocalWorkspace } from './native-workspace.mjs';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

const prior = '11111111-1111-4111-8111-111111111111';
const next = '22222222-2222-4222-8222-222222222222';
const context = { entry_id: '33333333-3333-4333-8333-333333333333', previous_journey_id: prior, journey_id: next };
const origin = 'http://bridge.localhost:17871';
const memory = () => { const values = new Map(); return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }; };
const locationAt = (value) => Object.assign({}, ...['origin', 'pathname', 'search', 'hash', 'href'].map((key) => ({ [key]: new URL(value)[key] })));
function fixture() {
  const channels = new Set(), messages = [], navigations = [], reads = [], owners = [];
  const state = { allowed: true, drop: () => false, sent: () => {} };
  const channelFactory = () => {
    const channel = { onmessage: null, close: () => channels.delete(channel), postMessage(data) {
      messages.push(structuredClone(data)); state.sent(data);
      if (state.drop(data)) return;
      for (const recipient of channels) if (recipient !== channel) queueMicrotask(() => {
        if (channels.has(recipient)) recipient.onmessage?.({ data: structuredClone(data) });
      });
    } }; channels.add(channel); return channel;
  };
  const fetch = async (path, options) => { reads.push({ path, options }); return new Response(JSON.stringify(state.allowed
    ? { state: 'selected', journey_id: next, previous_journey_id: prior } : { state: 'unconfirmed' }),
  { status: state.allowed ? 200 : 401, headers: { 'Content-Type': 'application/json' } }); };
  const options = { fetch, channelFactory, loadParser: async () => ({ parseOnboardingJson }), timeoutMs: 30 };
  const addOwner = () => {
    const location = locationAt(`${origin}/provisioning#journey=${prior}`), storage = memory(); let active = true, owner;
    const mount = () => { owner = createLocalWorkspaceOwner({ ...options, location, storage, current: () => active,
      retire: () => { active = false; owner.stop(); }, navigate: (url) => {
        navigations.push(url); Object.assign(location, locationAt(url)); active = true; mount();
      } }); owners.push(owner); };
    mount(); return { location, storage, stop: () => { active = false; owner.stop(); } };
  };
  const returnStorage = memory();
  const run = (extra = {}) => returnToLocalWorkspace({ ...options, context, storage: returnStorage,
    location: locationAt(`${origin}/provisioning/native-return#journey=${prior}`), discoveryMs: 5, ...extra });
  return { state, channels, messages, navigations, reads, addOwner, run, returnStorage,
    stop: () => { for (const owner of owners) owner.stop(); } };
}

test('one local owner authenticates before navigation and its new document authenticates before acknowledgement', async () => {
  const f = fixture(); f.addOwner();
  try {
    assert.deepEqual(await f.run(), { status: 'returned' });
    assert.deepEqual(f.navigations, [`${origin}/provisioning#journey=${next}`]);
    assert.ok(f.reads.length >= 5);
    assert.ok(f.reads.every(({ path, options }) => path === '/api/v1/provisioning/native-entry'
      && options.method === undefined && options.credentials === 'same-origin' && options.redirect === 'error'));
    assert.equal(f.messages.filter((message) => message.type === 'navigate').length, 1);
    assert.equal(JSON.stringify(f.messages).includes('csrf'), false);
  } finally { f.stop(); }
});

test('no responding owner continues locally and permanently ignores a late offer', async () => {
  const f = fixture();
  assert.deepEqual(await f.run(), { status: 'continued' });
  const before = f.messages.length; f.addOwner();
  assert.deepEqual(await f.run(), { status: 'continued' });
  assert.equal(f.messages.length, before); assert.deepEqual(f.navigations, []); f.stop();
});

test('multiple authenticated offers refuse without navigating any document', async () => {
  const f = fixture(); f.addOwner(); f.addOwner();
  try { await assert.rejects(f.run(), /unconfirmed/); assert.deepEqual(f.navigations, []); }
  finally { f.stop(); }
});

test('a changed owner route cannot accept its previously offered navigation', async () => {
  const f = fixture(); const owner = f.addOwner();
  f.state.sent = (message) => { if (message.type === 'offer') owner.location.href = `${origin}/other`; };
  try {
    await assert.rejects(f.run(), /unconfirmed/); await assert.rejects(f.run(), /unconfirmed/);
    assert.deepEqual(f.navigations, []); assert.equal(f.messages.filter((message) => message.type === 'navigate').length, 1);
  } finally { f.stop(); }
});

test('lost acknowledgement only reconciles the committed new document without navigation replay', async () => {
  const f = fixture(); f.addOwner(); f.state.drop = (message) => message.type === 'committed';
  try {
    await assert.rejects(f.run(), /unconfirmed/); assert.equal(f.navigations.length, 1);
    f.state.drop = () => false;
    assert.deepEqual(await f.run(), { status: 'returned' });
    assert.equal(f.navigations.length, 1);
    assert.equal(f.messages.filter((message) => message.type === 'navigate').length, 1);
    assert.equal(f.messages.filter((message) => message.type === 'reconcile').length, 1);
  } finally { f.stop(); }
});

test('expired selected authority after an offer prevents navigation and never becomes local fallback', async () => {
  const f = fixture(); f.addOwner();
  f.state.sent = (message) => { if (message.type === 'offer') f.state.allowed = false; };
  try {
    await assert.rejects(f.run(), /unconfirmed/); await assert.rejects(f.run(), /unconfirmed/);
    assert.deepEqual(f.navigations, []);
    assert.equal(f.messages.filter((message) => message.type === 'navigate').length, 1);
  } finally { f.stop(); }
});

test('missing selected authority cannot even discover another document', async () => {
  const f = fixture(); f.addOwner(); f.state.allowed = false;
  try { await assert.rejects(f.run(), /unconfirmed/); assert.deepEqual(f.messages, []); }
  finally { f.stop(); }
});

test('a callback retired during discovery never dispatches or adopts itself', async () => {
  const f = fixture(); f.addOwner(); let active = true;
  f.state.sent = (message) => { if (message.type === 'offer') active = false; };
  try {
    await assert.rejects(f.run({ current: () => active }), /unconfirmed/);
    assert.deepEqual(f.navigations, []); assert.equal(f.returnStorage.getItem('native_workspace_local_return_v1'), null);
  } finally { f.stop(); }
});
