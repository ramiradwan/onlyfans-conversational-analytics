import assert from 'node:assert/strict';
import test from 'node:test';

import { clearExtensionLocalData } from '../runtime/local-data.mjs';

function storageArea(values) {
  return {
    async clear() {
      for (const key of Object.keys(values)) delete values[key];
    },
  };
}

function indexedDbHarness(names) {
  const deleted = [];
  return {
    deleted,
    async databases() { return names.map((name) => ({ name })); },
    deleteDatabase(name) {
      const request = { error: null };
      deleted.push(name);
      queueMicrotask(() => request.onsuccess?.());
      return request;
    },
  };
}

test('local data deletion clears Chrome storage and every owned account database', async () => {
  const local = { consent: true, installation: true };
  const session = { binding: true };
  const accountA = `onlyfans-agent-account-v2-${'a'.repeat(64)}`;
  const accountB = `onlyfans-agent-account-v2-${'b'.repeat(64)}`;
  const indexedDb = indexedDbHarness([accountA, 'extension-cache', accountB]);

  assert.deepEqual(await clearExtensionLocalData({
    chromeApi: {
      storage: {
        local: storageArea(local),
        session: storageArea(session),
      },
    },
    indexedDb,
  }), { deleted_databases: 3 });
  assert.deepEqual(indexedDb.deleted, [accountA, 'extension-cache', accountB]);
  assert.deepEqual(local, {});
  assert.deepEqual(session, {});
});

test('local data deletion fails closed when database enumeration is unavailable', async () => {
  let cleared = false;
  await assert.rejects(
    clearExtensionLocalData({
      chromeApi: {
        storage: {
          local: { async clear() { cleared = true; } },
          session: { async clear() { cleared = true; } },
        },
      },
      indexedDb: {},
    }),
    /enumeration is unavailable/,
  );
  assert.equal(cleared, false);
});
