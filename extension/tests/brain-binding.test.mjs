import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ACTIVE_ACCOUNT_PARTITION_KEY,
  BRAIN_BINDING_MESSAGE_TYPE,
  createBrainBindingBridge,
  createChromeAdapter,
} from '../transport/chrome-adapter.mjs';
import { INGESTION_STORES } from '../transport/durable-outbox.mjs';
import { accountDatabaseName } from '../transport/indexeddb-ingestion-storage.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const clone = (value) => structuredClone(value);

function storageArea(values) {
  return {
    get(keys, callback) {
      callback(Object.fromEntries(
        keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, clone(values[key])]),
      ));
    },
    set(update, callback) {
      Object.assign(values, clone(update));
      callback?.();
    },
    remove(keys, callback) {
      for (const key of Array.isArray(keys) ? keys : [keys]) delete values[key];
      callback?.();
    },
  };
}

function harness() {
  const local = {};
  const session = {};
  const listeners = [];
  const indexedDb = new FakeIndexedDb();
  const chromeApi = {
    runtime: {
      onMessageExternal: {
        addListener(listener) { listeners.push(listener); },
        removeListener() {},
      },
    },
    storage: {
      local: storageArea(local),
      session: storageArea(session),
    },
  };
  const adapter = createChromeAdapter(chromeApi, () => 'installation-1', { indexedDb });
  let wakes = 0;
  const bridge = createBrainBindingBridge({
    chromeApi,
    adapter,
    runtime: { async wake() { wakes += 1; } },
  });
  bridge.register();
  return { adapter, bridge, indexedDb, listeners, local, session, wakes: () => wakes };
}

function binding(overrides = {}) {
  return {
    type: BRAIN_BINDING_MESSAGE_TYPE,
    protocol_version: '2',
    creator_account_id: 'creator-account-1',
    auth_ticket: 'purpose-bound-agent-ticket',
    ...overrides,
  };
}

function dispatch(listener, message, sender = { url: 'http://bridge.localhost:17871/settings' }) {
  return new Promise((resolve) => {
    const keepAlive = listener(message, sender, resolve);
    if (keepAlive === false) queueMicrotask(() => resolve(undefined));
  });
}

test('Brain binding accepts only an exact Bridge origin and keeps credentials out of global storage', async () => {
  const h = harness();
  assert.equal(h.listeners.length, 1);

  for (const url of [
    'http://bridge.localhost:17872/settings',
    'https://bridge.localhost:17871/settings',
    'http://localhost:17871/settings',
  ]) {
    assert.equal(await dispatch(h.listeners[0], binding(), { url }), undefined);
  }
  assert.deepEqual(h.session, {});
  assert.equal(h.wakes(), 0);

  assert.deepEqual(await dispatch(h.listeners[0], { ...binding(), unexpected: true }), {
    ok: false,
    code: 'invalid_binding',
  });
  assert.deepEqual(h.session, {});
  assert.equal(h.wakes(), 0);

  assert.deepEqual(await dispatch(h.listeners[0], binding()), { ok: true });
  assert.equal(await h.adapter.loadAgentInstallationId(), 'installation-1');
  const databaseName = await accountDatabaseName('creator-account-1');
  assert.deepEqual(h.local, { agent_installation_id: 'installation-1' });
  assert.deepEqual(h.session, {
    [ACTIVE_ACCOUNT_PARTITION_KEY]: databaseName,
  });
  assert.doesNotMatch(
    JSON.stringify({ local: h.local, session: h.session }),
    /creator-account|purpose-bound-agent-ticket/,
  );
  assert.deepEqual(
    h.indexedDb.databases
      .get(databaseName)
      .stores
      .get(INGESTION_STORES.credentials)
      .records
      .get('brain'),
    {
      key: 'brain',
      creator_account_id: 'creator-account-1',
      pairing_auth_ticket: 'purpose-bound-agent-ticket',
      reconnect_auth_ticket: null,
    },
  );
  assert.deepEqual(await h.adapter.loadBrainBinding(), {
    creatorAccountId: 'creator-account-1',
    authTicket: 'purpose-bound-agent-ticket',
  });
  assert.equal(h.wakes(), 1);
});

test('reconnect credentials survive same-account pairing but are cleared on switch and unbind', async () => {
  const h = harness();
  const firstDatabaseName = await accountDatabaseName('creator-account-1');
  const secondDatabaseName = await accountDatabaseName('creator-account-2');
  await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-1',
    authTicket: 'pairing-ticket-1',
  });
  await h.adapter.saveReconnectAuthTicket({
    creatorAccountId: 'creator-account-1',
    authTicket: 'reconnect-ticket-1',
  });

  await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-1',
    authTicket: 'pairing-ticket-2',
  });
  assert.equal(
    await h.adapter.loadReconnectAuthTicket('creator-account-1'),
    'reconnect-ticket-1',
  );
  assert.equal(h.session[ACTIVE_ACCOUNT_PARTITION_KEY], firstDatabaseName);
  assert.doesNotMatch(JSON.stringify(h.session), /creator-account|ticket/);

  await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-2',
    authTicket: 'pairing-ticket-3',
  });
  assert.equal(await h.adapter.loadReconnectAuthTicket('creator-account-1'), null);
  assert.equal(await h.adapter.loadReconnectAuthTicket('creator-account-2'), null);
  assert.equal(h.session[ACTIVE_ACCOUNT_PARTITION_KEY], secondDatabaseName);
  assert.equal(
    h.indexedDb.databases
      .get(firstDatabaseName)
      .stores
      .get(INGESTION_STORES.credentials)
      .records.size,
    0,
  );

  await h.adapter.saveReconnectAuthTicket({
    creatorAccountId: 'creator-account-2',
    authTicket: 'reconnect-ticket-2',
  });
  await h.adapter.clearBrainBinding();
  assert.deepEqual(h.session, {});
  assert.equal(
    h.indexedDb.databases
      .get(secondDatabaseName)
      .stores
      .get(INGESTION_STORES.credentials)
      .records.size,
    0,
  );
});
