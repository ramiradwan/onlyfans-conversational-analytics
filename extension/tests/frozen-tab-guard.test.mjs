import assert from 'node:assert/strict';
import test from 'node:test';

import {
  assertOnlyFansTabCanRun,
  guardMainWorldDispatch,
} from '../transport/read-only-frozen-tab-guard.mjs';

function chromeHarness(tab) {
  let dispatched = 0;
  return {
    chromeApi: {
      tabs: {
        async query() { return [{ active: true, ...tab }]; },
        async get() { return tab; },
      },
      scripting: {
        async executeScript() { dispatched += 1; return []; },
      },
    },
    dispatched: () => dispatched,
  };
}

test('history signing refuses frozen and pre-Chrome-132 tabs before minting', async () => {
  for (const tab of [{ id: 7, frozen: true }, { id: 7 }]) {
    const h = chromeHarness(tab);
    await assert.rejects(assertOnlyFansTabCanRun(h.chromeApi), /frozen state is unavailable or unsafe/);
  }
  const h = chromeHarness({ id: 7, frozen: false });
  assert.equal(await assertOnlyFansTabCanRun(h.chromeApi), 7);
});

test('main-world dispatch rechecks that the target tab is explicitly unfrozen', async () => {
  const frozen = chromeHarness({ id: 7, frozen: true });
  await assert.rejects(
    guardMainWorldDispatch(frozen.chromeApi).scripting.executeScript({ target: { tabId: 7 } }),
    /frozen state is unavailable or unsafe/,
  );
  assert.equal(frozen.dispatched(), 0);

  const available = chromeHarness({ id: 7, frozen: false });
  await guardMainWorldDispatch(available.chromeApi).scripting.executeScript({ target: { tabId: 7 } });
  assert.equal(available.dispatched(), 1);
});

test('an older frozen tab cannot hide a usable tab from preflight or signer selection', async () => {
  const candidates = [
    { id: 7, active: false, frozen: true },
    { id: 8, active: true, frozen: true },
    { id: 9, active: false },
    { id: 10, active: false, frozen: false },
  ];
  const calls = [];
  const tabs = {
    async query() { assert.equal(this, tabs); return candidates; },
    async get(id) { assert.equal(this, tabs); return candidates.find((tab) => tab.id === id); },
    async reload(id) { assert.equal(this, tabs); calls.push(id); },
  };
  const chromeApi = { tabs, scripting: { async executeScript() { calls.push('dispatch'); } } };
  assert.equal(await assertOnlyFansTabCanRun(chromeApi), 10);
  const guarded = guardMainWorldDispatch(chromeApi);
  assert.deepEqual(await guarded.tabs.query({ url: ['https://onlyfans.com/*'] }), [candidates[3]]);
  await guarded.tabs.reload(10);
  assert.deepEqual(calls, [10]);
  // The selected tab freezes between discovery and dispatch. No fallback is allowed.
  candidates[3].frozen = true;
  await assert.rejects(guarded.scripting.executeScript({ target: { tabId: 10 } }), {
    code: 'frozen_tab_unavailable',
  });
  assert.deepEqual(calls, [10]);
});
