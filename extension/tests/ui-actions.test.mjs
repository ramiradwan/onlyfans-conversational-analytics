import assert from 'node:assert/strict';
import test from 'node:test';

import { openCreatorAccount } from '../ui/actions.mjs';

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
