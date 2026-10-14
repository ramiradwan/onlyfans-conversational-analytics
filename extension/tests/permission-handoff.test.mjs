import assert from 'node:assert/strict';
import test from 'node:test';
import { createHandoffFinisher } from '../ui/handoff.mjs';
import { DESKTOP_HANDOFF_WINDOW_KEY } from '../runtime/ui-surfaces.mjs';

test('permission completion closes only the exact recorded compact window', async () => {
  const url = 'chrome-extension://fixture/options.html#history';
  globalThis.location = { href: url };
  for (const [type, windowId, savedUrl, expected] of [
    ['popup', 9, url, true], ['normal', 9, url, false], ['popup', 10, url, false], ['popup', 9, `${url}-other`, false],
  ]) {
    const calls = [];
    globalThis.chrome = {
      storage: { session: { async get() { return { [DESKTOP_HANDOFF_WINDOW_KEY]: { window_id: windowId, url: savedUrl } }; },
        async remove() { calls.push('cleared'); } } },
      windows: { async getCurrent() { return { id: 9, type }; }, async remove(id) { calls.push(id); } },
      tabs: { create() { assert.fail('permission completion must not create a dashboard tab'); } },
    };
    const finish = createHandoffFinisher();
    await Promise.all([finish(), finish()]);
    assert.deepEqual(calls, expected ? [9, 'cleared'] : []);
  }
});
