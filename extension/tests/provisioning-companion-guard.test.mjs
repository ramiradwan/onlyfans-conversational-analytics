import assert from 'node:assert/strict';
import test from 'node:test';

import { createProvisioningCompanionGuard } from '../runtime/provisioning-companion-guard.mjs';
import {
  PROVISIONING_IDENTITY_MESSAGE_TYPE,
  PROVISIONING_IDENTITY_VERSION,
} from '../transport/provisioning-identity.mjs';

const EXTENSION_ID = 'synthetic-extension-id';
const EPOCH_A = '10000000-0000-4000-8000-000000000001';
const EPOCH_B = '10000000-0000-4000-8000-000000000002';
const SENDER = Object.freeze({
  id: EXTENSION_ID,
  frameId: 0,
  url: 'https://onlyfans.com/my/chats',
  tab: { id: 17 },
  documentId: 'document-a',
  documentLifecycle: 'active',
});

function event() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) { listeners.push(listener); },
    removeListener(listener) {
      const index = listeners.indexOf(listener);
      if (index >= 0) listeners.splice(index, 1);
    },
  };
}

function observation(accountId, pageEpoch = EPOCH_A) {
  return {
    type: PROVISIONING_IDENTITY_MESSAGE_TYPE,
    version: PROVISIONING_IDENTITY_VERSION,
    page_epoch: pageEpoch,
    authenticated_profile: accountId === null ? null : { creator_account_id: accountId },
  };
}

function harness(configuredPlatformIdentity = () => null) {
  const onMessage = event();
  let invalidations = 0;
  const guard = createProvisioningCompanionGuard({
    chromeApi: { runtime: { id: EXTENSION_ID, onMessage } },
    companionClient: { invalidate() { invalidations += 1; } },
    configuredPlatformIdentity,
  });
  guard.register();
  return {
    guard,
    listener: onMessage.listeners[0],
    invalidations: () => invalidations,
    listenerCount: () => onMessage.listeners.length,
  };
}

test('same creator document churn does not tear down the companion but sign-out and switches do', () => {
  const h = harness();
  assert.equal(h.listener(observation('creator-a'), SENDER), false);
  assert.equal(h.invalidations(), 0);

  const nextDocument = { ...SENDER, documentId: 'document-b' };
  assert.equal(h.listener(observation('creator-a', EPOCH_B), nextDocument), false);
  assert.equal(h.invalidations(), 0);

  assert.equal(h.listener(observation('creator-b', EPOCH_B), nextDocument), false);
  assert.equal(h.invalidations(), 1);

  assert.equal(h.listener(observation(null, EPOCH_B), nextDocument), false);
  assert.equal(h.invalidations(), 2);
});

test('durable configured identity rejects the first conflicting observation after restart', () => {
  const h = harness(() => 'creator-a');
  assert.equal(h.listener(observation('creator-b'), SENDER), false);
  assert.equal(h.invalidations(), 1);

  assert.equal(h.listener(observation('creator-a', EPOCH_B), { ...SENDER, documentId: 'document-b' }), false);
  assert.equal(h.invalidations(), 1);
});

test('untrusted or malformed observations never control companion lifetime', () => {
  const h = harness(() => 'creator-a');
  assert.equal(h.listener(observation('creator-b'), { ...SENDER, url: 'https://example.test/' }), false);
  assert.equal(h.listener({ ...observation('creator-b'), extra: true }, SENDER), false);
  assert.equal(h.invalidations(), 0);

  h.guard.unregister();
  assert.equal(h.listenerCount(), 0);
});
