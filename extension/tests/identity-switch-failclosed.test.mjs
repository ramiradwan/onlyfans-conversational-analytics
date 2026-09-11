import assert from 'node:assert/strict';
import test from 'node:test';

import {
  PROVISIONING_IDENTITY_MESSAGE_TYPE,
  PROVISIONING_IDENTITY_STORAGE_KEY,
  createProvisioningIdentityBridge,
} from '../transport/provisioning-identity.mjs';
import { assertCaptureContext } from '../runtime/capture-context.mjs';

const EXTENSION_ID = 'synthetic-extension-id';
const SENDER = Object.freeze({
  id: EXTENSION_ID,
  frameId: 0,
  url: 'https://onlyfans.com/my/chats',
  tab: { id: 17 },
  documentId: 'document-a',
  documentLifecycle: 'active',
});
const EPOCH_A = '10000000-0000-4000-8000-000000000001';
const EPOCH_B = '10000000-0000-4000-8000-000000000002';

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

function dispatch(listener, message) {
  return new Promise((resolve) => {
    const keepAlive = listener(message, SENDER, resolve);
    assert.equal(keepAlive, true);
  });
}

function update(accountId, pageEpoch) {
  return {
    type: PROVISIONING_IDENTITY_MESSAGE_TYPE,
    version: 1,
    page_epoch: pageEpoch,
    authenticated_profile: accountId === null ? null : { creator_account_id: accountId },
  };
}

test('an account change immediately fences pending capture and requires the matching authorized account', async () => {
  const session = {};
  const internalListeners = [];
  const chromeApi = {
    runtime: {
      id: EXTENSION_ID,
      onMessage: {
        addListener(listener) { internalListeners.push(listener); },
        removeListener() {},
      },
      onMessageExternal: {
        addListener() {},
        removeListener() {},
      },
    },
    storage: {
      session: storageArea(session),
    },
  };
  const consent = { mode: 'full', consent_epoch: '10000000-0000-4000-8000-000000000008' };
  const bridge = createProvisioningIdentityBridge({ chromeApi, currentConsent: () => consent });
  bridge.register();

  assert.deepEqual(await dispatch(internalListeners[0], update('creator-a', EPOCH_A)), { ok: true });

  let releaseCapture;
  let started;
  const admitted = new Promise((resolve) => { started = resolve; });
  const heldCapture = bridge.withCaptureContext(SENDER, async (context, assertCurrent) => {
    assert.equal(context.observed_platform_id, 'creator-a');
    started();
    await new Promise((resolve) => { releaseCapture = resolve; });
    assertCurrent();
  });
  await admitted;
  let identityUpdateCompleted = false;
  const pendingIdentityUpdate = dispatch(internalListeners[0], update(null, EPOCH_B)).then((value) => {
    identityUpdateCompleted = true;
    return value;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(identityUpdateCompleted, true);
  releaseCapture();
  await assert.rejects(heldCapture, { code: 'stale_capture_context' });
  assert.deepEqual(await pendingIdentityUpdate, { ok: true });

  assert.deepEqual(await dispatch(internalListeners[0], update('creator-b', EPOCH_B)), { ok: true });

  const context = await bridge.contextFor(SENDER);
  assert.equal(context.identity_conflict, undefined);
  assert.equal(context.observed_platform_id, 'creator-b');
  assert.equal(
    session[PROVISIONING_IDENTITY_STORAGE_KEY].contexts[0].identity_conflict,
    undefined,
  );

  assert.throws(() => assertCaptureContext({
    sender: SENDER,
    extensionId: EXTENSION_ID,
    consent, delivery: { consent_epoch: consent.consent_epoch },
    context,
    configuration: {
      creator_account_id: 'companion-account-a',
      history_acquisition: {
        authorized_platform_creator_id: 'creator-a',
      },
    },
    boundCreatorAccountId: 'companion-account-a',
    observation: {
      page_epoch: EPOCH_B,
      creator_platform_user_id: 'creator-b',
    },
  }), { code: 'account_mismatch' });
});
