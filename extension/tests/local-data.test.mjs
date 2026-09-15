import assert from 'node:assert/strict';
import test from 'node:test';

import { clearExtensionLocalData } from '../runtime/local-data.mjs';
import { DELETE_INTENT_KEY } from '../runtime/deletion-state.mjs';

function storageArea(values) {
  return {
    async get(keys) {
      if (keys === null) return structuredClone(values);
      return Object.fromEntries(
        keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, structuredClone(values[key])]),
      );
    },
    async remove(keys) {
      for (const key of Array.isArray(keys) ? keys : [keys]) delete values[key];
    },
    async clear() {
      for (const key of Object.keys(values)) delete values[key];
    },
  };
}

function indexedDbHarness(names, failing = new Set()) {
  const deleted = [];
  return {
    deleted,
    async databases() { return names.map((name) => ({ name })); },
    deleteDatabase(name) {
      const request = { error: null };
      deleted.push(name);
      queueMicrotask(() => {
        if (failing.has(name)) {
          request.error = new Error(`delete failed: ${name}`);
          request.onerror?.();
        } else request.onsuccess?.();
      });
      return request;
    },
  };
}

test('local data deletion preserves the recovery marker until its caller completes cleanup', async () => {
  const local = {
    consent: true,
    installation: true,
    [DELETE_INTENT_KEY]: { schema: 'ofca-delete-intent/v1', requested_at: '2030-01-08T12:00:00Z' },
  };
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
  assert.deepEqual(local, {
    [DELETE_INTENT_KEY]: { schema: 'ofca-delete-intent/v1', requested_at: '2030-01-08T12:00:00Z' },
  });
  assert.deepEqual(session, {});
});

test('local data deletion attempts every database and storage cleanup after a database failure', async () => {
  const local = {
    consent: true,
    [DELETE_INTENT_KEY]: { schema: 'ofca-delete-intent/v1', requested_at: '2030-01-08T12:00:00Z' },
  };
  const session = { binding: true };
  const indexedDb = indexedDbHarness(['first', 'second', 'third'], new Set(['second']));

  await assert.rejects(
    clearExtensionLocalData({
      chromeApi: {
        storage: {
          local: storageArea(local),
          session: storageArea(session),
        },
      },
      indexedDb,
    }),
    (error) => {
      assert.equal(error.code, 'delete_incomplete');
      assert.deepEqual(error.failures, [{ stage: 'database_delete', database_name: 'second' }]);
      return true;
    },
  );
  assert.deepEqual(indexedDb.deleted, ['first', 'second', 'third']);
  assert.deepEqual(local, {
    [DELETE_INTENT_KEY]: { schema: 'ofca-delete-intent/v1', requested_at: '2030-01-08T12:00:00Z' },
  });
  assert.deepEqual(session, {});
});

test('local data deletion still clears Chrome storage when database enumeration is unavailable', async () => {
  const local = {
    consent: true,
    [DELETE_INTENT_KEY]: { schema: 'ofca-delete-intent/v1', requested_at: '2030-01-08T12:00:00Z' },
  };
  const session = { binding: true };

  await assert.rejects(
    clearExtensionLocalData({
      chromeApi: {
        storage: {
          local: storageArea(local),
          session: storageArea(session),
        },
      },
      indexedDb: {},
    }),
    (error) => error.code === 'delete_incomplete',
  );
  assert.deepEqual(local, {
    [DELETE_INTENT_KEY]: { schema: 'ofca-delete-intent/v1', requested_at: '2030-01-08T12:00:00Z' },
  });
  assert.deepEqual(session, {});
});
