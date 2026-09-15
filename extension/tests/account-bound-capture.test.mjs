import assert from 'node:assert/strict';
import test from 'node:test';

import {
  CAPTURE_DELIVERY_TYPE,
  CAPTURE_DELIVERY_VERSION,
} from '../capture/envelopes.mjs';
import {
  assertCaptureContext,
  captureSenderKey,
} from '../runtime/capture-context.mjs';
import { createAccountBoundCaptureMessageBridge } from '../transport/account-bound-capture-bridge.mjs';

const EXTENSION_ID = 'synthetic-extension-id';
const PAGE_EPOCH = '10000000-0000-4000-8000-000000000001';
const CONSENT = { mode: 'full', consent_epoch: '10000000-0000-4000-8000-000000000008' };
const DELIVERY_ID = '10000000-0000-4000-8000-000000000009';
const SENDER = Object.freeze({
  id: EXTENSION_ID,
  frameId: 0,
  url: 'https://onlyfans.com/my/chats',
  tab: { id: 17 },
  documentId: 'document-a',
  documentLifecycle: 'active',
});
const OBSERVATION = Object.freeze({
  event_type: 'chat.observed',
  observed_at: '2030-01-08T12:00:00Z',
  source_path: '/api2/v2/chats',
  creator_platform_user_id: 'creator-platform-a',
  context_chat_id: null,
  page_epoch: PAGE_EPOCH,
  record: {
    chat_id: 'chat-a',
    platform_user_id: 'fan-a',
    display_name: 'Fan A',
    updated_at: '2030-01-08T12:00:00Z',
  },
});
const CONFIGURATION = Object.freeze({
  creator_account_id: 'companion-account-a',
  history_acquisition: {
    authorized_platform_creator_id: 'creator-platform-a',
  },
});
const CONTEXT = Object.freeze({
  consent_epoch: CONSENT.consent_epoch,
  sender_key: '17:document-a',
  tab_id: 17,
  document_id: 'document-a',
  page_epoch: PAGE_EPOCH,
  observed_platform_id: 'creator-platform-a',
});

function runtimeMessage(observation = OBSERVATION) {
  return {
    type: CAPTURE_DELIVERY_TYPE,
    version: CAPTURE_DELIVERY_VERSION,
    delivery_id: DELIVERY_ID,
    consent_epoch: CONSENT.consent_epoch,
    created_at_ms: Date.parse('2030-01-08T12:00:00Z'),
    observation,
  };
}

function bridgeHarness({
  context = CONTEXT,
  configuration = CONFIGURATION,
  currentTransport = null,
  wakeError = null,
} = {}) {
  const listeners = [];
  const ingested = [];
  const drops = [];
  let wakeCalls = 0;
  const wakeTransport = { creatorAccountId: 'companion-account-a' };
  const runtime = {
    configuration: { activeDocument: configuration },
    transport: currentTransport,
    async wake() {
      wakeCalls += 1;
      if (wakeError !== null) throw wakeError;
      return wakeTransport;
    },
  };
  const ingestion = {
    rejectBridgeMessage() {
      return { ok: false, code: 'invalid_bridge_message', retryable: false };
    },
    async ingest(observation, options) {
      ingested.push({ observation: structuredClone(observation), options });
      return { ok: true, event_type: observation.event_type, source_seq: 1 };
    },
  };
  const chromeApi = {
    runtime: {
      id: EXTENSION_ID,
      onMessage: {
        addListener(listener) { listeners.push(listener); },
        removeListener() {},
      },
    },
  };
  const bridge = createAccountBoundCaptureMessageBridge({
    ingestion,
    runtime,
    provisioningIdentityBridge: {
      async withCaptureContext(_sender, work) {
        return work(context === null ? null : structuredClone(context));
      },
    },
    allowsCapture: () => true,
    currentConsent: () => CONSENT,
    diagnostics: {
      record(reason, eventType) { drops.push({ reason, eventType }); },
    },
    chromeApi,
  });
  bridge.register();
  return {
    drops,
    ingested,
    listener: listeners[0],
    runtime,
    get wakeCalls() { return wakeCalls; },
  };
}

function dispatch(listener, message = runtimeMessage(), sender = SENDER) {
  return new Promise((resolve) => {
    const keepAlive = listener(message, sender, resolve);
    if (keepAlive === false) queueMicrotask(() => resolve(undefined));
  });
}

test('capture sender identity includes the top-level document', () => {
  assert.equal(captureSenderKey(SENDER, EXTENSION_ID), '17:document-a');
  assert.throws(
    () => captureSenderKey({ ...SENDER, documentId: undefined }, EXTENSION_ID),
    { code: 'invalid_sender' },
  );
});

test('capture context requires the bound companion account and authorized platform identity', () => {
  assert.equal(assertCaptureContext({
    sender: SENDER,
    extensionId: EXTENSION_ID,
    consent: CONSENT, delivery: runtimeMessage(),
    context: CONTEXT,
    configuration: CONFIGURATION,
    boundCreatorAccountId: 'companion-account-a',
    observation: OBSERVATION,
  }), 'creator-platform-a');

  assert.throws(() => assertCaptureContext({
    sender: SENDER,
    extensionId: EXTENSION_ID,
    consent: CONSENT, delivery: runtimeMessage(),
    context: { ...CONTEXT, observed_platform_id: 'creator-platform-b' },
    configuration: CONFIGURATION,
    boundCreatorAccountId: 'companion-account-a',
    observation: OBSERVATION,
  }), { code: 'account_mismatch' });

  assert.throws(() => assertCaptureContext({
    sender: SENDER,
    extensionId: EXTENSION_ID,
    consent: CONSENT, delivery: runtimeMessage(),
    context: { ...CONTEXT, page_epoch: '10000000-0000-4000-8000-000000000002' },
    configuration: CONFIGURATION,
    boundCreatorAccountId: 'companion-account-a',
    observation: OBSERVATION,
  }), { code: 'stale_capture_context' });
});

test('account-bound bridge strips page context only after authorization succeeds', async () => {
  const matching = bridgeHarness();
  assert.deepEqual(await dispatch(matching.listener), {
    ok: true,
    event_type: 'chat.observed',
    source_seq: 1,
  });
  assert.equal(matching.ingested.length, 1);
  assert.equal(Object.hasOwn(matching.ingested[0].observation, 'page_epoch'), false);
  assert.equal(matching.ingested[0].observation.creator_platform_user_id, 'creator-platform-a');
  assert.equal(matching.ingested[0].options.delivery.delivery_id, DELIVERY_ID);

  const mismatched = bridgeHarness({
    context: { ...CONTEXT, observed_platform_id: 'creator-platform-b' },
  });
  assert.deepEqual(await dispatch(mismatched.listener), {
    ok: false,
    code: 'account_mismatch',
    retryable: false,
  });
  assert.deepEqual(mismatched.ingested, []);
});

test('initialized account runtime accepts locally without requiring a Brain wake', async () => {
  const transport = { creatorAccountId: 'companion-account-a' };
  const h = bridgeHarness({
    currentTransport: transport,
    wakeError: new Error('Brain unavailable'),
  });
  assert.deepEqual(await dispatch(h.listener), {
    ok: true,
    event_type: 'chat.observed',
    source_seq: 1,
  });
  assert.equal(h.wakeCalls, 0);
  assert.equal(h.ingested.length, 1);
  assert.equal(h.ingested[0].options.transport, transport);
  assert.deepEqual(h.drops, []);
});

test('uninitialized account runtime still requires a Brain wake before capture', async () => {
  const h = bridgeHarness({ wakeError: new Error('Brain unavailable') });
  assert.deepEqual(await dispatch(h.listener), {
    ok: false,
    code: 'enqueue_failed',
    retryable: true,
  });
  assert.equal(h.wakeCalls, 1);
  assert.deepEqual(h.ingested, []);
  assert.deepEqual(h.drops, [{ reason: 'enqueue_failed', eventType: 'chat.observed' }]);
});

test('account-bound bridge fails closed without an authorized platform identity', async () => {
  const h = bridgeHarness({
    configuration: {
      creator_account_id: 'companion-account-a',
      history_acquisition: { authorized_platform_creator_id: null },
    },
  });
  assert.deepEqual(await dispatch(h.listener), {
    ok: false,
    code: 'identity_required',
    retryable: false,
  });
  assert.deepEqual(h.ingested, []);
});

test('old deliveries cannot cross a consent generation', async () => {
  const h = bridgeHarness();
  assert.deepEqual(await dispatch(h.listener, { ...runtimeMessage(),
    consent_epoch: '10000000-0000-4000-8000-000000000007',
  }), { ok: false, code: 'stale_capture_context', retryable: false });
  assert.equal(h.ingested.length, 0);
});
