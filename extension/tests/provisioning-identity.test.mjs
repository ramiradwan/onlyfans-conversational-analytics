import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

import { parseIdentityResponse } from '../../app/provisioning/provisioning.js';
import {
  PROVISIONING_IDENTITY_MESSAGE_TYPE,
  PROVISIONING_IDENTITY_STORAGE_KEY,
  createProvisioningIdentityBridge,
} from '../transport/provisioning-identity.mjs';

const QUERY = Object.freeze({ type: 'provisioning.identity.query', version: 1 });
const BRIDGE_SENDER = Object.freeze({ url: 'http://bridge.localhost:17871/provisioning' });
const CONTENT_SENDER = Object.freeze({
  id: 'synthetic-extension-id',
  frameId: 0,
  url: 'https://onlyfans.com/my/chats',
});

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
  const bridge = createProvisioningIdentityBridge({ chromeApi });
  if (register) bridge.register();
  return { bridge, chromeApi, externalListeners, internalListeners, local, session };
}

function dispatch(listener, message, sender) {
  return new Promise((resolve) => {
    const keepAlive = listener(message, sender, resolve);
    if (keepAlive === false) queueMicrotask(() => resolve(undefined));
  });
}

test('hooked identity responses flow through the content bridge, clear on sign-out, and satisfy the page parser', async () => {
  const h = bridgeHarness();
  const pageListeners = [];
  const posts = [];
  let identityBody = { id: 'creator-from-platform' };
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
      return {
        clone() {
          return { async json() { return identityBody; } };
        },
      };
    },
    postMessage(message, targetOrigin) { posts.push({ message, targetOrigin }); },
    addEventListener(type, listener) {
      if (type === 'message') pageListeners.push(listener);
    },
  };
  const pageContext = vm.createContext({
    console,
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
    console,
    chrome: {
      runtime: {
        lastError: null,
        sendMessage(message, callback) {
          const listener = h.internalListeners[0];
          assert.equal(
            listener(message, CONTENT_SENDER, callback),
            true,
            'content forwards an identity update to the worker listener',
          );
        },
      },
    },
    window: pageWindow,
  });
  await Promise.all([
    readFile(new URL('../page-hook.js', import.meta.url), 'utf8').then((source) => (
      vm.runInContext(source, pageContext)
    )),
    readFile(new URL('../content.js', import.meta.url), 'utf8').then((source) => (
      vm.runInContext(source, contentContext)
    )),
  ]);

  await pageWindow.fetch('/api2/v2/users/me');
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(JSON.parse(JSON.stringify(posts[0])), {
    message: {
      type: PROVISIONING_IDENTITY_MESSAGE_TYPE,
      version: 1,
      authenticated_profile: { creator_account_id: 'creator-from-platform' },
    },
    targetOrigin: 'https://onlyfans.com',
  });
  pageListeners[0]({
    source: pageWindow,
    origin: 'https://onlyfans.com',
    data: posts[0].message,
  });
  await new Promise((resolve) => setImmediate(resolve));

  const signedIn = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(
    parseIdentityResponse(signedIn),
    { accountId: 'creator-from-platform' },
    'worker identity response satisfies provisioning.parseIdentityResponse',
  );

  identityBody = { user: null };
  await pageWindow.fetch('/api2/v2/init');
  await new Promise((resolve) => setImmediate(resolve));
  pageListeners[0]({
    source: pageWindow,
    origin: 'https://onlyfans.com',
    data: posts[1].message,
  });
  await new Promise((resolve) => setImmediate(resolve));

  const signedOut = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(
    parseIdentityResponse(signedOut),
    { accountId: null },
    'identity-path response with no user clears the provisioning-visible account',
  );
  assert.deepEqual(h.session, { [PROVISIONING_IDENTITY_STORAGE_KEY]: null });
  assert.deepEqual(h.local, {});
});

test('identity reads are exact-origin, binding-independent, and fail closed for absent or malformed session state', async () => {
  const h = bridgeHarness();
  const absent = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(absent), { accountId: null });

  h.session[PROVISIONING_IDENTITY_STORAGE_KEY] = { creator_account_id: 7 };
  const malformed = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(malformed), { accountId: null });

  h.session[PROVISIONING_IDENTITY_STORAGE_KEY] = [
    { creator_account_id: 'creator-a' },
    { creator_account_id: 'creator-b' },
  ];
  const ambiguous = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(ambiguous), { accountId: null });

  assert.equal(
    await dispatch(h.externalListeners[0], QUERY, { url: 'http://bridge.localhost:17872/provisioning' }),
    undefined,
    'exact-origin gate rejects a different localhost port',
  );
  assert.equal(
    await dispatch(h.externalListeners[0], { ...QUERY, extra: true }, BRIDGE_SENDER),
    undefined,
    'closed query envelope rejects extra fields',
  );
  assert.deepEqual(h.local, {});
});

test('identity updates require the extension content sender and replace the one session value on repeat', async () => {
  const h = bridgeHarness();
  const update = (creatorAccountId) => ({
    type: PROVISIONING_IDENTITY_MESSAGE_TYPE,
    version: 1,
    authenticated_profile: { creator_account_id: creatorAccountId },
  });
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
  assert.deepEqual(await dispatch(h.internalListeners[0], update('creator-b'), CONTENT_SENDER), { ok: true });
  const repeated = await dispatch(h.externalListeners[0], QUERY, BRIDGE_SENDER);
  assert.deepEqual(parseIdentityResponse(repeated), { accountId: 'creator-b' });
});

test('background registers the provisioning identity external responder', async () => {
  globalThis.crypto ??= webcrypto;
  const h = bridgeHarness({ register: false });
  const wakeEvents = () => ({ addListener() {}, removeListener() {} });
  globalThis.chrome = {
    ...h.chromeApi,
    alarms: {
      create() { return Promise.resolve(); },
      get: async () => undefined,
      onAlarm: wakeEvents(),
    },
    tabs: { onUpdated: wakeEvents() },
    runtime: {
      ...h.chromeApi.runtime,
      onInstalled: wakeEvents(),
      onStartup: wakeEvents(),
    },
  };
  await import(`../background.js?provisioning-identity-registration=${Date.now()}`);
  const replies = await Promise.all(h.externalListeners.map((listener) => (
    dispatch(listener, QUERY, BRIDGE_SENDER)
  )));
  const response = replies.find((candidate) => candidate?.type === 'provisioning.identity.result');
  assert.deepEqual(
    parseIdentityResponse(response),
    { accountId: null },
    'background registers provisioning identity external responder',
  );
});
