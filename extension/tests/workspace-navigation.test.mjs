import assert from 'node:assert/strict';
import test from 'node:test';
import { createWorkspaceNavigation } from '../ui/workspace-navigation.mjs';

const journey = '11111111-1111-4111-8111-111111111111';
const scope = { scope_id: '22222222-2222-4222-8222-222222222222', disclosure_bundle_id: 'a'.repeat(64) };
const ownUrl = `chrome-extension://synthetic/setup.html#journey=${journey}`;
const event = () => { const listeners = new Set(); return { addListener: (fn) => listeners.add(fn), emit: (value) => { for (const fn of listeners) fn(value); } }; };
function fixture() {
  const ports = [], navigations = [], lifecycle = {}, location = { href: ownUrl, replace: (target) => navigations.push(target) };
  const workspace = { journey_id: journey, draft_scope: { ...scope } };
  const runtime = { getURL: (name) => `chrome-extension://synthetic/${name}`, connect() {
    const port = { onMessage: event(), onDisconnect: event(), disconnect() { this.onDisconnect.emit(); } };
    ports.push(port); return port;
  } };
  const client = createWorkspaceNavigation({ workspace: () => workspace, location, runtime,
    window: { addEventListener: (name, listener) => { lifecycle[name] = listener; } } });
  const request = { type: 'navigate', request_id: crypto.randomUUID(), journey_id: journey,
    draft_scope: { ...scope }, expected_url: ownUrl, route: 'provisioning' };
  return { client, ports, navigations, lifecycle, location, workspace, request };
}
test('admitted setup document constructs only the registered local destination and ignores duplicate command', () => {
  const f = fixture(); f.ports[0].onMessage.emit(f.request); f.ports[0].onMessage.emit(f.request);
  assert.deepEqual(f.navigations, [`http://bridge.localhost:17871/provisioning#journey=${journey}`]);
  f.client.stop();
});
for (const change of ['user-navigation', 'scope', 'journey', 'extra-field', 'remote-route', 'old-port', 'hidden-lifecycle']) {
  test(`document navigation refuses ${change} without touching a replacement page`, () => {
    const f = fixture(); const old = f.ports[0];
    if (change === 'user-navigation') f.location.href = 'https://onlyfans.com/my/chats';
    if (change === 'scope') f.workspace.draft_scope.scope_id = crypto.randomUUID();
    if (change === 'journey') f.request.journey_id = crypto.randomUUID();
    if (change === 'extra-field') f.request.url = 'https://evil.example';
    if (change === 'remote-route') f.request.route = 'https://evil.example';
    if (change === 'hidden-lifecycle') f.lifecycle.pagehide();
    if (change === 'old-port') { f.lifecycle.pagehide(); f.lifecycle.pageshow(); }
    old.onMessage.emit(f.request);
    assert.deepEqual(f.navigations, []); f.client.stop();
  });
}

test('recovery navigates the admitted prior document to the selected journey once', () => {
  const f = fixture(); const next = crypto.randomUUID();
  const request = { ...f.request, type: 'recover', previous_journey_id: journey, journey_id: next };
  f.ports[0].onMessage.emit(request); f.ports[0].onMessage.emit(request);
  assert.deepEqual(f.navigations, [`http://bridge.localhost:17871/provisioning#journey=${next}`]); f.client.stop();
});
for (const change of ['prior', 'scope', 'document-url', 'runtime-route', 'invalid-target', 'extra-field']) {
  test(`recovery document refuses ${change}`, () => {
    const f = fixture(); const request = { ...f.request, type: 'recover', previous_journey_id: journey, journey_id: crypto.randomUUID() };
    if (change === 'prior') request.previous_journey_id = crypto.randomUUID();
    if (change === 'scope') f.workspace.draft_scope.scope_id = crypto.randomUUID();
    if (change === 'document-url') f.location.href += '&other=true';
    if (change === 'runtime-route') request.route = 'bridge';
    if (change === 'invalid-target') request.journey_id = 'invalid';
    if (change === 'extra-field') request.url = 'https://example.test/';
    f.ports[0].onMessage.emit(request); assert.deepEqual(f.navigations, []); f.client.stop();
  });
}
