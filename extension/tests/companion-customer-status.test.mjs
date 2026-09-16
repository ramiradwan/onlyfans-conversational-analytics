import assert from 'node:assert/strict';
import test from 'node:test';

import { createCompanionClient } from '../runtime/companion-client.mjs';

function storageArea() {
  const values = {};
  return {
    async get(keys) {
      return Object.fromEntries(keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, values[key]]));
    },
    async set(update) { Object.assign(values, update); },
    async remove(keys) { for (const key of keys) delete values[key]; },
  };
}

function chromeApi() {
  const event = () => ({ addListener() {}, removeListener() {} });
  return {
    runtime: {
      id: 'a'.repeat(32),
      getURL: (path) => `chrome-extension://${'a'.repeat(32)}/${path}`,
      onConnect: event(), onStartup: event(), onInstalled: event(), onMessage: event(),
    },
    storage: { local: storageArea(), session: storageArea() },
    tabs: { onUpdated: event() },
    alarms: { onAlarm: event(), async create() {} },
  };
}

test('Full status exposes setup incomplete when no signed-in creator account is available', async () => {
  let storeReads = 0;
  const client = createCompanionClient({
    chromeApi: chromeApi(),
    allowsFull: () => true,
    detectedAccountId: async () => null,
    accountDatabaseName: async (id) => `account-${id}`,
    storeFactory: async () => ({
      async status() { storeReads += 1; return { paired: false }; },
      close() {},
    }),
    loadSnow: async () => { throw new Error('crypto_must_not_load_for_status'); },
    channelFactory: async () => { throw new Error('network_must_not_open_for_status'); },
    wireFactory: async () => { throw new Error('network_must_not_open_for_status'); },
    loadTrust: async () => { throw new Error('trust_must_not_load_for_status'); },
  });

  assert.deepEqual(await client.status(), { state: 'setup_incomplete', comparison_code: null });
  assert.equal(storeReads, 1);
});

test('Preview status remains unavailable without reading pairing storage or creator identity', async () => {
  let touched = false;
  const client = createCompanionClient({
    chromeApi: chromeApi(),
    allowsFull: () => false,
    detectedAccountId: async () => { touched = true; return 'creator'; },
    accountDatabaseName: async (id) => `account-${id}`,
    storeFactory: async () => { touched = true; return { async status() { return { paired: false }; }, close() {} }; },
  });

  assert.deepEqual(await client.status(), { state: 'unavailable', comparison_code: null });
  assert.equal(touched, false);
});
