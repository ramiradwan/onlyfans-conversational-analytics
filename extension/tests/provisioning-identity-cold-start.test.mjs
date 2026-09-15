import assert from 'node:assert/strict';
import test from 'node:test';

import {
  PROVISIONING_IDENTITY_STORAGE_KEY,
  PROVISIONING_IDENTITY_STORAGE_SCHEMA,
  createProvisioningIdentityBridge,
} from '../transport/provisioning-identity.mjs';

const PERSISTED_EPOCH = '10000000-0000-4000-8000-000000000008';
const DEFAULT_EPOCH = '10000000-0000-4000-8000-000000000009';
const PAGE_EPOCH = '10000000-0000-4000-8000-000000000001';

function event() {
  const listeners = [];
  return {
    addListener(listener) { listeners.push(listener); },
    removeListener(listener) {
      const index = listeners.indexOf(listener);
      if (index >= 0) listeners.splice(index, 1);
    },
    emit(...args) { for (const listener of [...listeners]) listener(...args); },
  };
}

function storageArea(values) {
  return {
    get(keys, callback) {
      callback(Object.fromEntries(
        keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, values[key]]),
      ));
    },
    set(update, callback) {
      Object.assign(values, structuredClone(update));
      callback?.();
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness() {
  const session = {
    [PROVISIONING_IDENTITY_STORAGE_KEY]: {
      schema: PROVISIONING_IDENTITY_STORAGE_SCHEMA,
      contexts: [{
        sender_key: '17:document-a',
        tab_id: 17,
        document_id: 'document-a',
        page_epoch: PAGE_EPOCH,
        observed_platform_id: 'creator-a',
        consent_epoch: PERSISTED_EPOCH,
      }],
    },
  };
  const onRemoved = event();
  const onUpdated = event();
  let consent = { mode: 'full', consent_epoch: DEFAULT_EPOCH };
  let readyCalls = 0;
  const chromeApi = {
    runtime: {
      id: 'synthetic-extension-id',
      lastError: null,
      onMessage: event(),
      onMessageExternal: event(),
      onStartup: event(),
      onInstalled: event(),
    },
    storage: { session: storageArea(session) },
    tabs: { onRemoved, onUpdated },
  };
  const bridge = createProvisioningIdentityBridge({
    chromeApi,
    currentConsent: () => consent,
    ensureReady: async () => {
      readyCalls += 1;
      consent = { mode: 'full', consent_epoch: PERSISTED_EPOCH };
    },
  });
  bridge.register();
  return { bridge, onRemoved, onUpdated, session, readyCalls: () => readyCalls };
}

test('cold-worker unrelated tab removal cannot erase persisted identity before consent restore', async () => {
  const h = harness();

  h.onRemoved.emit(99);
  await flush();
  await flush();

  assert.equal(h.readyCalls(), 1);
  assert.deepEqual(h.session[PROVISIONING_IDENTITY_STORAGE_KEY], {
    schema: PROVISIONING_IDENTITY_STORAGE_SCHEMA,
    contexts: [{
      sender_key: '17:document-a',
      tab_id: 17,
      document_id: 'document-a',
      page_epoch: PAGE_EPOCH,
      observed_platform_id: 'creator-a',
      consent_epoch: PERSISTED_EPOCH,
    }],
  });
  assert.equal(await h.bridge.currentAccountId(), 'creator-a');
});

test('cold-worker removal still deletes the removed tab context after consent restore', async () => {
  const h = harness();

  h.onRemoved.emit(17);
  await flush();
  await flush();

  assert.equal(h.readyCalls(), 1);
  assert.deepEqual(h.session[PROVISIONING_IDENTITY_STORAGE_KEY], {
    schema: PROVISIONING_IDENTITY_STORAGE_SCHEMA,
    contexts: [],
  });
  assert.equal(await h.bridge.currentAccountId(), null);
});
