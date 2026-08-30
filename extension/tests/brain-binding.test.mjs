import assert from 'node:assert/strict';
import test from 'node:test';

import * as fullAdapter from '../transport/chrome-adapter.mjs';
import * as readOnlyAdapter from '../transport/read-only-chrome-adapter.mjs';
import {
  ACTIVE_ACCOUNT_PARTITION_KEY,
  BRAIN_BINDING_MESSAGE_TYPE,
  FULL_STORAGE_BOOTSTRAP_KEY,
} from '../transport/chrome-adapter.mjs';
import {
  INGESTION_DATABASE_NAME_PREFIX,
  LEGACY_INGESTION_DATABASE_NAME_PREFIX,
  accountDatabaseName,
} from '../transport/indexeddb-ingestion-storage.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const STORAGE_KEY = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';
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

function brainStorageApi() {
  const unlocks = new Map([
    ['sealed-pairing-1', {
      schema: 'ofca-extension-storage-unlock/v1',
      creator_account_id: 'creator-account-1',
      credential_kind: 'pairing',
      auth_ticket: 'purpose-bound-agent-ticket',
      storage_key_base64: STORAGE_KEY,
    }],
    ['sealed-pairing-2', {
      schema: 'ofca-extension-storage-unlock/v1',
      creator_account_id: 'creator-account-2',
      credential_kind: 'pairing',
      auth_ticket: 'purpose-bound-agent-ticket-2',
      storage_key_base64: STORAGE_KEY,
    }],
    ['sealed-pairing-1b', {
      schema: 'ofca-extension-storage-unlock/v1',
      creator_account_id: 'creator-account-1',
      credential_kind: 'pairing',
      auth_ticket: 'purpose-bound-agent-ticket-new',
      storage_key_base64: STORAGE_KEY,
    }],
  ]);
  const calls = [];
  return {
    calls,
    async fetch(url, options) {
      const body = JSON.parse(options.body);
      calls.push({ url, options: clone(options), body });
      if (url.endsWith('/unseal')) {
        const value = unlocks.get(body.storage_bootstrap);
        return value
          ? { ok: true, async json() { return clone(value); } }
          : { ok: false, async json() { return {}; } };
      }
      if (url.endsWith('/rotate')) {
        const current = unlocks.get(body.storage_bootstrap);
        if (!current || current.creator_account_id !== body.creator_account_id) {
          return { ok: false, async json() { return {}; } };
        }
        const sealed = `sealed-reconnect-${body.creator_account_id}`;
        unlocks.set(sealed, {
          ...current,
          credential_kind: 'reconnect',
          auth_ticket: body.reconnect_auth_ticket,
        });
        return {
          ok: true,
          async json() {
            return {
              schema: 'ofca-extension-storage-rotation/v1',
              storage_bootstrap: sealed,
            };
          },
        };
      }
      throw new Error(`Unexpected endpoint ${url}`);
    },
  };
}

function harness({ module = fullAdapter, onBound, state = null } = {}) {
  const local = state?.local ?? {};
  const session = state?.session ?? {};
  const listeners = [];
  const indexedDb = state?.indexedDb ?? new FakeIndexedDb();
  const brain = state?.brain ?? brainStorageApi();
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
  const adapter = module.createChromeAdapter(chromeApi, () => 'installation-1', {
    indexedDb,
    fetchImpl: brain.fetch,
    listDatabases: async () => [...indexedDb.databases.keys()].map((name) => ({ name })),
  });
  let wakes = 0;
  const bridge = module.createBrainBindingBridge({
    chromeApi,
    adapter,
    runtime: { async wake() { wakes += 1; } },
    onBound,
  });
  bridge.register();
  return {
    adapter,
    brain,
    bridge,
    indexedDb,
    listeners,
    local,
    session,
    state: { brain, indexedDb, local, session },
    wakes: () => wakes,
  };
}

function binding(overrides = {}) {
  return {
    type: BRAIN_BINDING_MESSAGE_TYPE,
    protocol_version: '2',
    creator_account_id: 'creator-account-1',
    auth_ticket: 'purpose-bound-agent-ticket',
    storage_bootstrap: 'sealed-pairing-1',
    ...overrides,
  };
}

function dispatch(listener, message, sender = { url: 'http://bridge.localhost:17871/settings' }) {
  return new Promise((resolve) => {
    const keepAlive = listener(message, sender, resolve);
    if (keepAlive === false) queueMicrotask(() => resolve(undefined));
  });
}

test('Brain binding accepts only the exact Bridge origin and persists only a sealed bootstrap', async () => {
  const h = harness();
  assert.equal(h.listeners.length, 1);

  for (const url of [
    'http://bridge.localhost:17872/settings',
    'https://bridge.localhost:17871/settings',
    'http://localhost:17871/settings',
  ]) {
    assert.equal(await dispatch(h.listeners[0], binding(), { url }), undefined);
  }
  assert.deepEqual(h.local, {});
  assert.equal(h.wakes(), 0);

  assert.deepEqual(await dispatch(h.listeners[0], { ...binding(), unexpected: true }), {
    ok: false,
    code: 'invalid_binding',
  });
  assert.deepEqual(await dispatch(h.listeners[0], binding()), { ok: true });
  assert.equal(await h.adapter.loadAgentInstallationId(), 'installation-1');
  const databaseName = await accountDatabaseName('creator-account-1');
  assert.deepEqual(h.local, {
    [FULL_STORAGE_BOOTSTRAP_KEY]: 'sealed-pairing-1',
    agent_installation_id: 'installation-1',
  });
  assert.deepEqual(h.session, { [ACTIVE_ACCOUNT_PARTITION_KEY]: databaseName });
  assert.doesNotMatch(
    JSON.stringify({ local: h.local, session: h.session }),
    /creator-account|purpose-bound-agent-ticket/,
  );
  assert.deepEqual(await h.adapter.loadBrainBinding(), {
    creatorAccountId: 'creator-account-1',
    authTicket: 'purpose-bound-agent-ticket',
    credentialKind: 'pairing',
    storageKey: STORAGE_KEY,
  });
  assert.equal(h.wakes(), 1);
});

test('binding deletes every legacy plaintext partition before enabling Full mode', async () => {
  const h = harness();
  const encryptedName1 = await accountDatabaseName('creator-account-1');
  const encryptedName2 = await accountDatabaseName('creator-account-2');
  assert.ok(encryptedName1.startsWith(`${INGESTION_DATABASE_NAME_PREFIX}-`));
  assert.ok(encryptedName2.startsWith(`${INGESTION_DATABASE_NAME_PREFIX}-`));
  const legacyName1 = `${LEGACY_INGESTION_DATABASE_NAME_PREFIX}${encryptedName1.slice(INGESTION_DATABASE_NAME_PREFIX.length)}`;
  const legacyName2 = `${LEGACY_INGESTION_DATABASE_NAME_PREFIX}${encryptedName2.slice(INGESTION_DATABASE_NAME_PREFIX.length)}`;
  h.indexedDb.databases.set(legacyName1, { version: 4, stores: new Map() });
  h.indexedDb.databases.set(legacyName2, { version: 4, stores: new Map() });
  h.indexedDb.databases.set('extension-cache', { version: 1, stores: new Map() });

  await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-1',
    authTicket: 'purpose-bound-agent-ticket',
    storageBootstrap: 'sealed-pairing-1',
  });

  assert.equal(h.indexedDb.databases.has(legacyName1), false);
  assert.equal(h.indexedDb.databases.has(legacyName2), false);
  assert.equal(h.indexedDb.databases.has('extension-cache'), true);
  assert.equal(h.local[FULL_STORAGE_BOOTSTRAP_KEY], 'sealed-pairing-1');
});

test('reconnect bootstrap rotates under session authority and survives a worker restart', async () => {
  const h = harness();
  await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-1',
    authTicket: 'purpose-bound-agent-ticket',
    storageBootstrap: 'sealed-pairing-1',
  });
  assert.equal(await h.adapter.loadReconnectAuthTicket('creator-account-1'), null);

  await h.adapter.saveReconnectAuthTicket({
    creatorAccountId: 'creator-account-1',
    authTicket: 'reconnect-ticket-1',
    configAuthTicket: 'config-ticket-1',
    agentInstallationId: 'installation-1',
  });
  assert.equal(h.local[FULL_STORAGE_BOOTSTRAP_KEY], 'sealed-reconnect-creator-account-1');
  const rotation = h.brain.calls.find((call) => call.url.endsWith('/rotate'));
  assert.equal(rotation.options.headers.Authorization, 'Bearer config-ticket-1');

  const restarted = harness({ state: h.state });
  assert.equal(
    await restarted.adapter.loadReconnectAuthTicket('creator-account-1'),
    'reconnect-ticket-1',
  );
  assert.deepEqual(await restarted.adapter.loadBrainBinding(), {
    creatorAccountId: 'creator-account-1',
    authTicket: 'reconnect-ticket-1',
    credentialKind: 'reconnect',
    storageKey: STORAGE_KEY,
  });
});

test('same-account dashboard pairing does not replace a durable reconnect bootstrap', async () => {
  const h = harness();
  await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-1',
    authTicket: 'purpose-bound-agent-ticket',
    storageBootstrap: 'sealed-pairing-1',
  });
  await h.adapter.saveReconnectAuthTicket({
    creatorAccountId: 'creator-account-1',
    authTicket: 'reconnect-ticket-1',
    configAuthTicket: 'config-ticket-1',
    agentInstallationId: 'installation-1',
  });

  const rebound = await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-1',
    authTicket: 'purpose-bound-agent-ticket-new',
    storageBootstrap: 'sealed-pairing-1b',
  });

  assert.equal(h.local[FULL_STORAGE_BOOTSTRAP_KEY], 'sealed-reconnect-creator-account-1');
  assert.equal(await h.adapter.loadReconnectAuthTicket('creator-account-1'), 'reconnect-ticket-1');
  assert.equal(rebound.credentialKind, 'reconnect');
  assert.equal(rebound.authTicket, 'reconnect-ticket-1');
});

test('account switch replaces only the opaque active bootstrap and clear removes it', async () => {
  const h = harness();
  await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-1',
    authTicket: 'purpose-bound-agent-ticket',
    storageBootstrap: 'sealed-pairing-1',
  });
  await h.adapter.saveBrainBinding({
    creatorAccountId: 'creator-account-2',
    authTicket: 'purpose-bound-agent-ticket-2',
    storageBootstrap: 'sealed-pairing-2',
  });
  assert.equal(h.local[FULL_STORAGE_BOOTSTRAP_KEY], 'sealed-pairing-2');
  assert.equal(await h.adapter.loadReconnectAuthTicket('creator-account-1'), null);
  await h.adapter.clearBrainBinding();
  assert.deepEqual(h.session, {});
  assert.equal(h.local[FULL_STORAGE_BOOTSTRAP_KEY], undefined);
});

test('both adapter builds agree on the binding message type', () => {
  assert.equal(readOnlyAdapter.BRAIN_BINDING_MESSAGE_TYPE, BRAIN_BINDING_MESSAGE_TYPE);
});

for (const [build, module] of [
  ['chrome-adapter', fullAdapter],
  ['read-only-chrome-adapter', readOnlyAdapter],
]) {
  test(`${build} hands runtime lifecycle to onBound instead of waking directly`, async () => {
    let bound = 0;
    const h = harness({ module, onBound: async () => { bound += 1; } });

    assert.deepEqual(await dispatch(h.listeners[0], binding()), { ok: true });
    assert.equal(bound, 1);
    assert.equal(h.wakes(), 0);
  });

  test(`${build} wakes the runtime when no onBound is supplied`, async () => {
    const h = harness({ module });
    assert.deepEqual(await dispatch(h.listeners[0], binding()), { ok: true });
    assert.equal(h.wakes(), 1);
  });

  test(`${build} reports agent_start_failed when onBound rejects`, async () => {
    const h = harness({ module, onBound: async () => { throw new Error('reconcile failed'); } });
    assert.deepEqual(await dispatch(h.listeners[0], binding()), {
      ok: false,
      code: 'agent_start_failed',
    });
  });

  test(`${build} refuses a non-callable onBound`, () => {
    assert.throws(
      () => harness({ module, onBound: 'reconcile' }),
      /onBound must be a function/,
    );
  });
}
