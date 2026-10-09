import assert from 'node:assert/strict';
import test from 'node:test';
import { createDesktopLaunch, desktopLaunchJourney } from '../ui/desktop-launch.mjs';
import { workspaceAppLink } from '../runtime/onboarding-native-launch.mjs';

const record = { journey_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fed', draft_scope: {
  scope_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fea', disclosure_bundle_id: 'a'.repeat(64),
} };
const connected = { desktopRuntimeReachable: true, status: { brain_reachable: true, delivery: { transport_state: 'authenticated' } } };
function fixture(prepare = async () => ({ journey_id: record.journey_id, app_link: workspaceAppLink(record.journey_id) })) {
  const calls = [], timers = new Map(); let current = structuredClone(record);
  const controller = createDesktopLaunch({ workspace: () => current, prepare,
    dispatch: (url) => calls.push(['dispatch', url]), navigate: async () => calls.push(['navigate']), changed: () => {},
    setTimer: (callback, ms) => { timers.set(1, { callback, ms }); return 1; }, clearTimer: (id) => timers.delete(id) });
  return { controller, calls, timers, changeScope: () => { current.draft_scope.scope_id = crypto.randomUUID(); } };
}
test('only an explicit click dispatches once; fallback does not mean launch failed or retry', async () => {
  const f = fixture(); f.controller.observe({ desktopLinked: true }); assert.deepEqual(f.calls, []);
  await f.controller.launch({}); await f.controller.launch({});
  assert.deepEqual(f.calls, [['dispatch', workspaceAppLink(record.journey_id)]]);
  assert.equal(f.controller.state, 'opening');
  f.timers.get(1).callback(); assert.equal(f.controller.state, 'unconfirmed');
  assert.equal(f.calls.length, 1);
  const card = desktopLaunchJourney({ id: 'desktop_app_needed' }, 'unconfirmed');
  assert.match(card.body, /Start menu/); assert.equal(card.primaryAction, 'open_desktop');
});
test('focus, HTTP hints and desktop ports cannot confirm launch; delayed authenticated transport continues automatically', async () => {
  const f = fixture(); await f.controller.launch({}); f.timers.get(1).callback();
  for (const model of [{ desktopLinked: true }, { desktopRuntimeReachable: true },
    { status: { brain_reachable: true, delivery: { transport_state: 'connected' } } }]) f.controller.observe(model);
  assert.equal(f.controller.state, 'unconfirmed'); assert.equal(f.calls.length, 1);
  f.controller.observe(connected); f.controller.observe(connected);
  assert.equal(f.controller.state, 'continuing');
  assert.deepEqual(f.calls[1], ['navigate']); assert.equal(f.calls.length, 2);
});
test('an already authenticated desktop retains the registered HTTP workspace route without app link', async () => {
  const f = fixture(); await f.controller.launch(connected);
  assert.deepEqual(f.calls, [['navigate']]);
});
test('scope change and page retirement fence a late launch preparation', async () => {
  for (const retire of ['scope', 'page']) {
    let resolve; const prepared = new Promise((done) => { resolve = done; });
    const f = fixture(() => prepared); const pending = f.controller.launch({});
    if (retire === 'scope') { f.changeScope(); f.controller.observe({}); } else f.controller.stop();
    resolve({ journey_id: record.journey_id, app_link: workspaceAppLink(record.journey_id) }); await pending;
    assert.deepEqual(f.calls, []); assert.equal(f.controller.state, 'idle');
  }
});
test('malformed preparation cannot open arbitrary URLs or embed extra authority', async () => {
  for (const result of [{ journey_id: record.journey_id, app_link: 'https://evil.test' },
    { journey_id: record.journey_id, app_link: workspaceAppLink(record.journey_id), cookie: 'forbidden' }]) {
    const f = fixture(async () => result); await f.controller.launch({});
    assert.deepEqual(f.calls, []); assert.equal(f.controller.state, 'unconfirmed');
  }
});
