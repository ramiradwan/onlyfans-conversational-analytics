import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { build } from 'esbuild';

import { parseIdentityResponse } from '../../app/provisioning/provisioning.js';
import {
  PROVISIONING_IDENTITY_MESSAGE_TYPE,
  PROVISIONING_IDENTITY_STORAGE_KEY,
  PROVISIONING_IDENTITY_STORAGE_SCHEMA,
  createProvisioningIdentityBridge,
} from '../transport/provisioning-identity.mjs';

const CONSENT = { mode: 'full', consent_epoch: '10000000-0000-4000-8000-000000000008' };
const QUERY = Object.freeze({ type: 'provisioning.identity.query', version: 1 });
const BRIDGE_SENDER = Object.freeze({ url: 'https://bridge.localhost:17871/provisioning' });
const CONTENT_SENDER = Object.freeze({
  id: 'synthetic-extension-id',
  frameId: 0,
  url: 'https://onlyfans.com/my/chats',
  tab: { id: 17 },
  documentId: 'document-a',
  documentLifecycle: 'active',
});
const PAGE_EPOCH_A = '10000000-0000-4000-8000-000000000001';
const PAGE_EPOCH_B = '10000000-0000-4000-8000-000000000002';
const PAGE_EPOCH_C = '10000000-0000-4000-8000-000000000003';

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

function bridgeHarness({ register = true } = {}) {
  const session = {};
  const local = {};
  const internalListeners = [];
  const externalListeners = [];
  const chromeApi = {
    runtime: {
      id: CONTENT_SENDER.id,
      onMessage: {
        addListener(listener) { internalListeners.push(listener); },
        removeListener(listener) {
          const index = internalListeners.indexOf(listener);
          if (index >= 0) internalListeners.splice(index, 1);
        },
      },
      onMessageExternal: {
        addListener(listener) { externalListeners.push(listener); },
        removeListener(listener) {
          const index = externalListeners.indexOf(listener);
          if (index >= 0) externalListeners.splice(index, 1);
        },
      },
    },
    storage: {
      local: storageArea(local),
      session: storageArea(session),
    },
  };
  const bridge = createProvisioningIdentityBridge({ chromeApi, currentConsent: () => CONSENT });
  if (register) bridge.register();
  return { bridge, chromeApi, externalListeners, internalListeners, local, session };
}

function dispatch(listener, message, sender) {
  return new Promise((resolve) => {
    const keepAlive = listener(message, sender, resolve);
    if (keepAlive === false) queueMicrotask(() => resolve(undefined));
  });
}

async function bundledSource(relativePath) {
  const result = await build({
    entryPoints: [fileURLToPath(new URL(relativePath, import.meta.url))],
    bundle: true,
    format: 'iife',
    platform: 'browser',
    target: ['chrome132'],
    write: false,
  });
  return result.outputFiles[0].text;
}

function update(accountId, pageEpoch = PAGE_EPOCH_A) {
  return {
    type: PROVISIONING_IDENTITY_MESSAGE_TYPE,
    version: 1,
    page_epoch: pageEpoch,
    authenticated_profile: accountId === null ? null : { creator_account_id: accountId },
  };
}

test('hooked identity responses bind the observed account to the current document', async () => {
  const h = bridgeHarness();
  const pageListeners = [];
  const posts = [];
  const dispatchPageMessage = (message) => {
    const event = {
      source: pageWindow,
      origin: 'https://onlyfans.com',
      data: message,
    };
    for (const listener of [...pageListeners]) listener(event);
  };
  let identityBody = { id: 'creator-from-platform' };
  const uuids = [PAGE_EPOCH_A, PAGE_EPOCH_B, PAGE_EPOCH_C];
  class FakeWebSocket {
    addEventListener() {}
  }
  class FakeXmlHttpRequest {
    open() {}
    send() {}
    addEventListener() {}
  }
  const pageWindow = {
    location: {
      origin: 'https://onlyfans.com',
      href: 'https://onlyfans.com/my/chats',
    },
    WebSocket: FakeWebSocket,
    async fetch() {
      return new Response(JSON.stringify(identityBody));
    },
    postMessage(message, targetOrigin) { posts.push({ message: structuredClone(message), targetOrigin }); },
    addEventListener(type, listener) {
      if (type === 'message') pageListeners.push(listener);
    },
  };
  const pageContext = vm.createContext({
    __OFCA_CAPTURE_MODE__: 'identity',
    console, TextEncoder, TextDecoder, AbortController, setTimeout, clearTimeout, structuredClone,
    crypto: { randomUUID: () => uuids.shift() },
    Date,
    JSON,
    Proxy,
    Reflect,
    URL,
    WeakMap,
    XMLHttpRequest: FakeXmlHttpRequest,
    window: pageWindow,
  });
  const contentContext = vm.createContext({
    console, TextEncoder, TextDecoder, AbortController, setTimeout, clearTimeout, structuredClone,
    chrome: {
      runtime: {
        lastError: null,
        onMessage: { addListener() {} },
        sendMessage(message, callback) {
          if (message.type === 'ofca.capture.context.query') {
            callback({ ok: true, consent_epoch: CONSENT.consent_epoch });
            return;
          }
          const listener = h.internalListeners[0];
          assert.equal(listener(message, CONTENT_SENDER, callback), true);
        },
      },
    },
    window: pageWindow,
  });
  await Promise.all([
    bundledSource('../page-hook.js').then((source) => vm.runInContext(source, pageContext)),
    bundledSource('../content.js').then((source) => vm.runInContext(source, contentContext)),
  ]);

  await pageWindow.fetch('/api2/v2/users/me');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(posts[0].message.type, PROVISIONING_IDENTITY_MESSAGE_TYPE);
  assert.equal(posts[0].message.page_epoch, PAGE_EPOCH_B);
  assert.deepEqual(posts[0].message.authenticated_profile, {
    creator_account_id: 'creator-from-platform',
  });
  dispatchPageMessage(posts[0].message);
  await new Promise((resolve) => setImmediate(resolve));

  const signedIn = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(signedIn), { accountId: 'creator-from-platform' });
  assert.deepEqual(await h.bridge.contextFor(CONTENT_SENDER), {
    sender_key: '17:document-a',
    tab_id: 17,
    document_id: 'document-a',
    page_epoch: PAGE_EPOCH_B,
    observed_platform_id: 'creator-from-platform',
    consent_epoch: CONSENT.consent_epoch,
  });

  identityBody = { user: null };
  await pageWindow.fetch('/api2/v2/init');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(posts[1].message.page_epoch, PAGE_EPOCH_C);
  dispatchPageMessage(posts[1].message);
  await new Promise((resolve) => setImmediate(resolve));

  const signedOut = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(signedOut), { accountId: null });
  assert.equal(
    h.session[PROVISIONING_IDENTITY_STORAGE_KEY].schema,
    PROVISIONING_IDENTITY_STORAGE_SCHEMA,
  );
  assert.equal(h.session[PROVISIONING_IDENTITY_STORAGE_KEY].contexts.length, 1);
  assert.equal(
    h.session[PROVISIONING_IDENTITY_STORAGE_KEY].contexts[0].observed_platform_id,
    null,
  );
  assert.deepEqual(h.local, {});
});

test('identity reads fail closed for malformed or ambiguous document state', async () => {
  const h = bridgeHarness();
  const absent = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(absent), { accountId: null });

  h.session[PROVISIONING_IDENTITY_STORAGE_KEY] = { creator_account_id: 7 };
  const malformed = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(malformed), { accountId: null });

  h.session[PROVISIONING_IDENTITY_STORAGE_KEY] = {
    schema: PROVISIONING_IDENTITY_STORAGE_SCHEMA,
    contexts: [
      {
        sender_key: '17:document-a',
        tab_id: 17,
        document_id: 'document-a',
        page_epoch: PAGE_EPOCH_A,
        observed_platform_id: 'creator-a',
    consent_epoch: CONSENT.consent_epoch,
      },
      {
        sender_key: '18:document-b',
        tab_id: 18,
        document_id: 'document-b',
        page_epoch: PAGE_EPOCH_B,
        observed_platform_id: 'creator-b',
    consent_epoch: CONSENT.consent_epoch,
      },
    ],
  };
  const ambiguous = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(ambiguous), { accountId: null });

  assert.equal(
    await dispatch(h.externalListeners[0], QUERY, { url: 'https://bridge.localhost:17872/provisioning' }),
    undefined,
  );
  assert.equal(
    await dispatch(h.externalListeners[0], { ...QUERY, extra: true }, BRIDGE_SENDER),
    undefined,
  );
});

test('identity updates are document-bound and replace stale context for the same tab', async () => {
  const h = bridgeHarness();
  assert.equal(
    h.internalListeners[0](update('forged'), { ...CONTENT_SENDER, url: 'https://example.test/' }, () => {}),
    false,
  );
  assert.equal(
    h.internalListeners[0]({ ...update('creator-a'), extra: true }, CONTENT_SENDER, () => {}),
    false,
  );
  assert.deepEqual(h.session, {});

  assert.deepEqual(await dispatch(h.internalListeners[0], update('creator-a'), CONTENT_SENDER), { ok: true });
  const nextDocument = {
    ...CONTENT_SENDER,
    documentId: 'document-b',
    url: 'https://onlyfans.com/my/chats?switched=1',
  };
  assert.deepEqual(
    await dispatch(h.internalListeners[0], update('creator-b', PAGE_EPOCH_B), nextDocument),
    { ok: true },
  );
  assert.equal(await h.bridge.contextFor(CONTENT_SENDER), null);
  assert.deepEqual(await h.bridge.contextFor(nextDocument), {
    sender_key: '17:document-b',
    tab_id: 17,
    document_id: 'document-b',
    page_epoch: PAGE_EPOCH_B,
    observed_platform_id: 'creator-b',
    consent_epoch: CONSENT.consent_epoch,
  });
  const repeated = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(repeated), { accountId: 'creator-b' });
});

test('background binds capture to provisioning identity before consent-controlled ingestion', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');
  assert.match(source, /createAccountBoundCaptureMessageBridge/);
  assert.match(source, /provisioningIdentityBridge,\s*\n\s*allowsCapture/);
  assert.match(source, /provisioningIdentityBridge,\s*\n\s*previewMetrics/);
});

test('identity changes fence admitted writes immediately and permit the new document context', async () => {
  const h = bridgeHarness();
  await dispatch(h.internalListeners[0], update('creator-a'), CONTENT_SENDER);
  let release;
  let started;
  const admitted = new Promise((resolve) => { started = resolve; });
  const work = h.bridge.withCaptureContext(CONTENT_SENDER, async (_context, assertCurrent) => {
    started();
    await new Promise((resolve) => { release = resolve; });
    assertCurrent();
  });
  await admitted;
  await dispatch(h.internalListeners[0], update('creator-b', PAGE_EPOCH_B), CONTENT_SENDER);
  release();
  await assert.rejects(work, { code: 'stale_capture_context' });
  assert.equal((await h.bridge.contextFor(CONTENT_SENDER)).observed_platform_id, 'creator-b');
  assert.equal(parseIdentityResponse(await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER)).accountId, 'creator-b');
});

test('clearing consent contexts fences admitted captures before session persistence settles', async () => {
  const h = bridgeHarness();
  await dispatch(h.internalListeners[0], update('creator-a'), CONTENT_SENDER);
  let guard;
  await h.bridge.withCaptureContext(CONTENT_SENDER, async (_context, assertCurrent) => { guard = assertCurrent; });
  const clearing = h.bridge.clearContexts();
  assert.throws(guard, { code: 'stale_capture_context' });
  await clearing;
  assert.equal(await h.bridge.contextFor(CONTENT_SENDER), null);
});
