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
        async query() { return [{ id: 7, active: true }]; },
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
