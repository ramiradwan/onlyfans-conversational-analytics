import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ACTIVE_ACCOUNT_PARTITION_KEY,
  PartitionAwareConsentController,
} from '../runtime/partition-aware-consent-controller.mjs';

function controllerHarness() {
  const controller = new PartitionAwareConsentController({
    chromeApi: {
      storage: { local: {}, session: {}, onChanged: {} },
      scripting: {},
      permissions: {},
    },
    runtime: { wake() {} },
    adapter: { loadBrainBinding() {}, clearBrainBinding() {} },
    provisioningIdentityBridge: {},
    previewMetrics: { record() {}, summary() {}, clear() {}, prune() {} },
    clearLocalData() {},
    activeModeAuthorization: {
      authorizeTransition() {},
      authorizeResume() {},
      reconcileActiveMode() {},
    },
  });
  let reconciliations = 0;
  controller.reconcile = () => {
    reconciliations += 1;
    return Promise.resolve();
  };
  return { controller, reconciliations: () => reconciliations };
}

test('initial account partition publication does not tear down Full mode', async () => {
  const h = controllerHarness();
  h.controller.storageListener({
    [ACTIVE_ACCOUNT_PARTITION_KEY]: { newValue: 'encrypted-account-a' },
  }, 'session');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.reconciliations(), 0);

  h.controller.storageListener({
    [ACTIVE_ACCOUNT_PARTITION_KEY]: {
      oldValue: 'encrypted-account-a',
      newValue: 'encrypted-account-a',
    },
  }, 'session');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.reconciliations(), 0);
});

test('established account partition replacement or removal remains fail-closed', async () => {
  const h = controllerHarness();
  h.controller.storageListener({
    [ACTIVE_ACCOUNT_PARTITION_KEY]: {
      oldValue: 'encrypted-account-a',
      newValue: 'encrypted-account-b',
    },
  }, 'session');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.reconciliations(), 1);

  h.controller.storageListener({
    [ACTIVE_ACCOUNT_PARTITION_KEY]: {
      oldValue: 'encrypted-account-b',
      newValue: undefined,
    },
  }, 'session');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.reconciliations(), 2);
});
