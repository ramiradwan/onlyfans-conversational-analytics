import fixtureRule from './fixtures/packaged-signing-rule.json' with { type: 'json' };
import { createChromeBrowserSigningProvider } from 'local-authenticated-read-connector/browser-signing';
import { guardMainWorldDispatch } from '../transport/read-only-frozen-tab-guard.mjs';

export const EXPECTED_ID = '9001';
export const TIMESTAMP = Date.parse('2026-09-11T12:00:00.000Z');
export const RULE = structuredClone(fixtureRule);
export const PRIVATE_MARKER = 'synthetic-private-field-must-not-cross-signer-boundary';
const ORIGIN = 'https://onlyfans.com';
const assert = {
  equal(actual, expected, message = 'Synthetic signer fixture invariant failed') {
    if (!Object.is(actual, expected)) throw new Error(message);
  },
  deepEqual(actual, expected) {
    if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error('Synthetic signer fixture structure differs');
  },
};

// Independent synthetic server arithmetic. This does not invoke the signer's signing code.
export async function signSyntheticRequest(rule, time, pathname, identity) {
  const bytes = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(
    [rule.static_param, time, pathname, identity].join('\n'),
  ));
  const digest = [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
  const checksum = Math.abs(rule.format.checksum_indexes.reduce(
    (sum, index) => sum + digest.charCodeAt(index), rule.format.checksum_constant,
  ));
  return [rule.format.prefix, digest, checksum.toString(16), rule.format.suffix].join(':');
}

export function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function event() {
  const listeners = new Set();
  return {
    addListener(listener) { listeners.add(listener); },
    removeListener(listener) { listeners.delete(listener); },
    emit(details) { for (const listener of [...listeners]) listener(details); },
    get size() { return listeners.size; },
  };
}

/** Installed signer behind the consumer's real frozen-tab proxy; only Chrome/network/storage are doubles. */
export function createSignerReleaseFixture({ initialState = null, expectedIdentity = EXPECTED_ID } = {}) {
  let saved = structuredClone(initialState);
  let serial = 0;
  let documentId = 'synthetic-document-0';
  const calls = { loads: 0, saves: 0, reloads: 0, reads: [], aborts: [], captures: [] };
  const webRequest = Object.fromEntries(['onBeforeSendHeaders', 'onHeadersReceived', 'onCompleted', 'onErrorOccurred']
    .map((name) => [name, event()]));
  const f = {
    calls, webRequest,
    accountId: EXPECTED_ID,
    tab: { id: 17, active: false, frozen: false },
    safeRefresh: { safe: true, reason: null },
    beforeDispatch: async () => {},
    response: (body, status = 200, extra = {}) => ({
      status, contentType: 'application/json', responseRevision: RULE.source_revision, body, ...extra,
    }),
    snapshot: () => structuredClone(saved),
    replaceStoredState(value) { saved = structuredClone(value); },
    currentDocument: () => documentId,
    navigate() { documentId = `synthetic-document-${++serial}`; },
    reply(request) {
      if (request.operation === 'identity') return f.response({ id: f.accountId });
      return f.response({ list: [], hasMore: false });
    },
    controlReply: () => f.response({ error: 'synthetic-signature-rejection' }, 400),
  };
  f.persistence = {
    async load() { calls.loads += 1; return structuredClone(saved); },
    async save(state) { calls.saves += 1; saved = structuredClone(state); },
  };
  const tabs = {
    async query(query) {
      assert.equal(this, tabs);
      assert.deepEqual(query, { url: [`${ORIGIN}/*`] });
      return [structuredClone(f.tab)];
    },
    async get(tabId) { assert.equal(this, tabs); assert.equal(tabId, f.tab.id); return structuredClone(f.tab); },
    async reload(tabId) {
      assert.equal(this, tabs); assert.equal(tabId, f.tab.id);
      calls.reloads += 1;
      f.navigate();
      const details = { requestId: `native-${calls.reloads}`, tabId, frameId: 0, documentId,
        documentLifecycle: 'active', method: 'GET', url: `${ORIGIN}/api2/v2/users/me` };
      const headers = {
        accept: 'application/json', 'app-token': 'synthetic-app-token', 'x-bc': 'synthetic-browser-context',
        'x-of-rev': RULE.source_revision, time: String(TIMESTAMP), cookie: PRIVATE_MARKER,
        sign: await signSyntheticRequest(RULE, String(TIMESTAMP), '/api2/v2/users/me', '0'),
      };
      calls.captures.push(structuredClone(headers));
      const pairs = (value) => Object.entries(value).map(([name, value]) => ({ name, value }));
      webRequest.onBeforeSendHeaders.emit({ ...details, requestHeaders: pairs(headers) });
      webRequest.onHeadersReceived.emit({ ...details, statusCode: 200, responseHeaders: pairs({
        'x-of-rev': RULE.source_revision, 'content-type': 'application/json',
      }) });
      webRequest.onCompleted.emit({ ...details, statusCode: 200 });
    },
  };
  const scripting = {
    async executeScript(details) {
      assert.equal(this, scripting, 'consumer Promise proxy must retain Chrome method receiver');
      await f.beforeDispatch(details);
      const currentDocument = documentId;
      if (details.func.name === 'inspectSigningDocumentInPage') {
        assert.equal(details.world, 'ISOLATED');
        return [{ frameId: 0, documentId: currentDocument, result: true }];
      }
      assert.equal(details.world, 'MAIN');
      assert.deepEqual(details.target, { tabId: f.tab.id, documentIds: [currentDocument] });
      let result;
      if (details.func.name === 'inspectSafeRefreshInPage') result = structuredClone(f.safeRefresh);
      else if (details.func.name === 'abortTypedReadInPage') {
        calls.aborts.push(details.args[0]); result = true;
      } else {
        assert.equal(details.func.name, 'executeTypedReadInPage');
        const [request] = details.args;
        assert.equal(request.method, 'GET');
        const url = new URL(request.url);
        assert.equal(url.origin, ORIGIN);
        calls.reads.push(structuredClone(request));
        const signature = await signSyntheticRequest(RULE, request.headers.time,
          `${url.pathname}${url.search}`, request.headers['user-id']);
        result = await (request.headers.sign === signature ? f.reply(request) : f.controlReply(request));
      }
      return [{ frameId: 0, documentId: currentDocument, result }];
    },
  };
  f.chromeApi = new Proxy({ tabs, scripting, webRequest, runtime: { lastError: null } }, {
    get(target, key) {
      if (key === 'storage') throw new Error('Custom account persistence cannot access global Chrome storage');
      return Reflect.get(target, key);
    },
  });
  f.createProvider = (options = {}) => createChromeBrowserSigningProvider({
    chromeApi: guardMainWorldDispatch(f.chromeApi, { signal: options.signal }),
    persistence: f.persistence,
    expectedIdentity,
    packagedRule: structuredClone(RULE),
    now: () => TIMESTAMP,
    idFactory: () => `synthetic-${++serial}`,
    ...options,
  });
  return f;
}
