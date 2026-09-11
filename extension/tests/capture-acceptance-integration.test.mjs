import assert from 'node:assert/strict';
import test from 'node:test';
import { createAccountBoundCaptureMessageBridge } from '../transport/account-bound-capture-bridge.mjs';
import { createProvisioningIdentityBridge } from '../transport/provisioning-identity.mjs';
import { OperationScope } from '../runtime/operation-scope.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

const epoch = '10000000-0000-4000-8000-000000000001';
const sender = { id: 'test-extension', frameId: 0, tab: { id: 7 }, documentId: 'test-document',
  documentLifecycle: 'active', url: 'https://onlyfans.com/my/chats' };
const identity = (account = 'platform-a') => ({ type: 'ofca.provisioning.identity.update', version: 1,
  page_epoch: epoch, authenticated_profile: { creator_account_id: account } });
const delivery = () => ({ type: 'ofca.capture.delivery', version: 1, delivery_id: crypto.randomUUID(),
  consent_epoch: epoch, created_at_ms: Date.now(), observation: {
    event_type: 'chat.observed', observed_at: '2030-01-01T00:00:00Z', source_path: '/api2/v2/chats',
    creator_platform_user_id: 'platform-a', context_chat_id: null, page_epoch: epoch,
    record: { chat_id: 'chat-a', platform_user_id: 'fan-a', display_name: 'Synthetic', updated_at: '2030-01-01T00:00:00Z' },
  },
});
const dispatch = (listener, message) => new Promise((resolve) => {
  assert.equal(listener(message, sender, resolve), true);
});

for (const prefix of ['', 'read-only-']) {
  const { DurableIngestOutbox } = await import(`../transport/${prefix}durable-outbox.mjs`);
  const { DeliveryCaptureIngestionService } = await import(`../transport/${prefix}delivery-capture-ingestion.mjs`);
  async function harness() {
    const session = {};
    const listeners = [];
    const chromeApi = { runtime: { id: sender.id,
      onMessage: { addListener: (listener) => listeners.push(listener) },
      onMessageExternal: { addListener() {} },
    }, storage: { session: {
      get(keys, callback) { callback(Object.fromEntries(keys.map((key) => [key, session[key]]))); },
      set(values, callback) { Object.assign(session, structuredClone(values)); callback(); },
    } } };
    const consent = { mode: 'full', consent_epoch: epoch };
    const provisioning = createProvisioningIdentityBridge({ chromeApi, currentConsent: () => consent });
    provisioning.register();
    await dispatch(listeners[0], identity());
    const storage = new InMemoryIngestionStorage();
    const outbox = new DurableIngestOutbox({ storage, creatorAccountId: 'companion-a' });
    await outbox.initialize();
    const transport = { outbox, creatorAccountId: 'companion-a', async flushOutbox() { throw new Error('offline'); } };
    const configuration = { creator_account_id: 'companion-a',
      history_acquisition: { authorized_platform_creator_id: 'platform-a' },
      capture_policy: { rules: [{ enabled: true, resource: 'chats', url_pattern: '/api2/v2/chats' }] },
    };
    const runtime = { transport, configuration: { activeDocument: configuration }, async wake() { return transport; } };
    const diagnostics = { record() {} };
    const ingestion = new DeliveryCaptureIngestionService({ runtime, diagnostics });
    const operationScope = new OperationScope(); operationScope.reopen();
    const bridge = createAccountBoundCaptureMessageBridge({ ingestion, runtime, chromeApi, diagnostics,
      provisioningIdentityBridge: provisioning, operationScope, currentConsent: () => consent,
      allowsCapture: () => consent.mode === 'full',
    });
    return { storage, outbox, bridge, configuration,
      switchIdentity: () => dispatch(listeners[0], identity('platform-b')) };
  }

  test(`${prefix || 'authoring-'}integrated local ACK succeeds while companion flush is offline`, async () => {
    const h = await harness(); const message = delivery();
    const accepted = await dispatch(h.bridge.listener, message);
    assert.equal(accepted.ok, true); assert.equal(accepted.accepted, true);
    assert.equal(h.storage.stores.get('delivery_receipts').size, 1);
    assert.deepEqual(await dispatch(h.bridge.listener, message), accepted);
    assert.equal(h.outbox.meta.last_source_seq, 1);
  });

  for (const race of ['identity', 'policy']) {
    test(`${prefix || 'authoring-'}integrated ${race} changes at receipt write roll back acceptance`, async () => {
      const h = await harness();
      const run = h.storage.runTransaction.bind(h.storage);
      let identityUpdate;
      h.storage.runTransaction = (mode, stores, work) => run(mode, stores, (tx) => work({ ...tx,
        put(store, value, key) {
          tx.put(store, value, key);
          if (store === 'delivery_receipts') {
            if (race === 'identity') identityUpdate = h.switchIdentity();
            else h.configuration.capture_policy.rules[0].enabled = false;
          }
        },
      }));
      const rejected = await dispatch(h.bridge.listener, delivery());
      await identityUpdate;
      assert.equal(rejected.ok, false);
      assert.equal(rejected.code, race === 'identity' ? 'stale_capture_context' : 'capture_disabled');
      for (const store of ['delivery_receipts', 'chats', 'outbox']) assert.equal(h.storage.stores.get(store).size, 0);
    });
  }
}
