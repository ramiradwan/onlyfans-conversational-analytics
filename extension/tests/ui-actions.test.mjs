import assert from 'node:assert/strict';
import test from 'node:test';

import { openCreatorAccount, allowHistory } from '../ui/actions.mjs';

test('openCreatorAccount focuses an existing OnlyFans tab', async () => {
  const calls = [];
  globalThis.chrome = {
    tabs: {
      async query() { return [{ id: 9, windowId: 4, active: false }]; },
      async update(id, options) { calls.push(['tab', id, options]); },
      async create() { calls.push(['create']); },
    },
    windows: {
      async update(id, options) { calls.push(['window', id, options]); },
    },
  };

  await openCreatorAccount();

  assert.deepEqual(calls, [
    ['tab', 9, { active: true }],
    ['window', 4, { focused: true }],
  ]);
});
test('openCreatorAccount opens OnlyFans when no tab exists', async () => {
  const calls = [];
  globalThis.chrome = {
    tabs: {
      async query() { return []; },
      async create(options) { calls.push(options); },
    },
    windows: {},
  };

  await openCreatorAccount();

  assert.deepEqual(calls, [{ url: 'https://onlyfans.com/' }]);
});

test('concurrent creator actions share one tab creation', async () => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  let created = 0, queried = 0;
  const chromeApi = { tabs: { async query() { queried++; await waiting; return []; },
    async create() { created++; } } };
  const first = openCreatorAccount(chromeApi), second = openCreatorAccount(chromeApi);
  release();
  await Promise.all([first, second]);
  assert.equal(queried, 1);
  assert.equal(created, 1);
});

test('history permission grants browser access without opening desktop pages in Chrome', async () => {
  const requested = [];
  let current = 'choice-A';
  globalThis.chrome = { runtime: { async sendMessage() { return { ok: true, result: { revision: current } }; } },
    permissions: { async request(value) { requested.push(value); return true; } },
    tabs: { create() { assert.fail('native history settings must stay native'); } } };
  await assert.rejects(allowHistory(null), /desktop app/);
  await assert.rejects(allowHistory('previous-choice'), /desktop app/);
  assert.deepEqual(requested, []);
  await allowHistory('choice-A');
  assert.deepEqual(requested, [{ permissions: ['webRequest'], origins: ['https://onlyfans.com/*'] }]);
  globalThis.chrome.permissions.request = async () => false;
  await assert.rejects(allowHistory('choice-A'), /not allowed/);
  current = null;
  await assert.rejects(allowHistory('choice-A'), /desktop app/);
});
