import assert from 'node:assert/strict';
import test from 'node:test';
import { createDesktopLaunch, desktopLaunchPresentation } from '../ui/desktop-launch.mjs';
import { workspaceAppLink } from '../runtime/onboarding-native-launch.mjs';

const record = { journey_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fed', draft_scope: {
  scope_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fea', disclosure_bundle_id: 'a'.repeat(64),
} };
function fixture(prepare = async () => ({ journey_id: record.journey_id, app_link: workspaceAppLink(record.journey_id) })) {
  const calls = [], timers = new Map(); const current = structuredClone(record);
  const controller = createDesktopLaunch({ workspace: () => current, prepare,
    dispatch: url => calls.push(url), changed: () => {},
    setTimer: (callback, ms) => { timers.set(1, { callback, ms }); return 1; }, clearTimer: id => timers.delete(id) });
  return { controller, calls, timers, changeScope: () => { current.draft_scope.scope_id = crypto.randomUUID(); } };
}

test('dispatch and transport hints cannot confirm desktop adoption or trigger another launch', async () => {
  const f = fixture();
  await f.controller.launch(); await f.controller.launch();
  assert.deepEqual(f.calls, [workspaceAppLink(record.journey_id)]);
  for (const hint of [undefined, { desktopLinked: true }, { desktopRuntimeReachable: true },
    { status: { brain_reachable: true, delivery: { transport_state: 'authenticated' } } }]) f.controller.observe(hint);
  assert.equal(f.controller.state, 'opening');
  f.timers.get(1).callback(); assert.equal(f.controller.state, 'unconfirmed');
  assert.equal(f.calls.length, 1);
  assert.equal(desktopLaunchPresentation('unconfirmed').title, 'Desktop connection not confirmed');
  f.controller.observe(true); assert.equal(f.controller.state, 'adopted');
  assert.equal(f.calls.length, 1);
});

test('restored adoption is presentation evidence and still requires a click to open the app', async () => {
  const f = fixture(); f.controller.observe(true);
  assert.equal(f.controller.state, 'adopted'); assert.deepEqual(f.calls, []);
  await f.controller.launch(); assert.deepEqual(f.calls, [workspaceAppLink(record.journey_id)]);
  assert.equal(f.controller.state, 'adopted'); assert.equal(f.timers.size, 0);
});

test('scope changes and page retirement fence a delayed preparation', async () => {
  for (const retire of ['scope', 'page']) {
    let resolve; const prepared = new Promise(done => { resolve = done; });
    const f = fixture(() => prepared); const pending = f.controller.launch();
    if (retire === 'scope') { f.changeScope(); f.controller.observe(false); } else f.controller.stop();
    resolve({ journey_id: record.journey_id, app_link: workspaceAppLink(record.journey_id) }); await pending;
    assert.deepEqual(f.calls, []); assert.equal(f.controller.state, 'idle');
  }
});

test('preparation cannot substitute a URL or add authority fields', async () => {
  for (const result of [{ journey_id: record.journey_id, app_link: 'https://evil.test' },
    { journey_id: record.journey_id, app_link: workspaceAppLink(record.journey_id), cookie: 'forbidden' }]) {
    const f = fixture(async () => result); await f.controller.launch();
    assert.deepEqual(f.calls, []); assert.equal(f.controller.state, 'unconfirmed');
  }
});
