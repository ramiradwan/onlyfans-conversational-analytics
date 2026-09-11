import assert from 'node:assert/strict';
import test from 'node:test';

import {
  LOCAL_SERVICE_ORIGIN,
  assertLocalServiceUrl,
} from '../transport/local-service-endpoints.mjs';
import { createSecureLocalFetch } from '../transport/secure-local-fetch.mjs';
import { requiredOriginsForMode } from '../runtime/permission-recovery.mjs';

test('local service endpoints require the exact HTTPS origin', () => {
  assert.equal(assertLocalServiceUrl(`${LOCAL_SERVICE_ORIGIN}/health`).origin, LOCAL_SERVICE_ORIGIN);
  assert.throws(() => assertLocalServiceUrl('http://bridge.localhost:17871/health'), /invalid_local_service_endpoint/);
  assert.throws(() => assertLocalServiceUrl('https://other.localhost:17871/health'), /invalid_local_service_endpoint/);
  assert.deepEqual(requiredOriginsForMode('full'), [
    'https://onlyfans.com/*',
    'https://bridge.localhost:17871/*',
  ]);
});

test('secure local fetch rejects redirects and bounds response bodies', async () => {
  let observedInit = null;
  const secure = createSecureLocalFetch({
    maxResponseBytes: 4,
    fetchImpl: async (_url, init) => {
      observedInit = init;
      return new Response('12345', { status: 200 });
    },
  });
  await assert.rejects(
    secure(`${LOCAL_SERVICE_ORIGIN}/health`),
    /local_service_response_too_large/,
  );
  assert.equal(observedInit.redirect, 'error');
  assert.equal(observedInit.credentials, 'omit');
  assert.equal(observedInit.referrerPolicy, 'no-referrer');
});

test('secure local fetch refuses HTTP before making a request', async () => {
  let calls = 0;
  const secure = createSecureLocalFetch({
    fetchImpl: async () => {
      calls += 1;
      return new Response('{}', { status: 200 });
    },
  });
  await assert.rejects(
    secure('http://bridge.localhost:17871/health'),
    /invalid_local_service_endpoint/,
  );
  assert.equal(calls, 0);
});


test('deadline bounds a stalled response body even when headers arrived', async () => {
  let cancelled = false;
  const fetch = createSecureLocalFetch({
    timeoutMs: 15,
    fetchImpl: async () => new Response(new ReadableStream({
      pull() { return new Promise(() => {}); },
      cancel() { cancelled = true; },
    })),
  });
  await assert.rejects(fetch(`${LOCAL_SERVICE_ORIGIN}/health`), { code: 'local_service_timeout' });
  assert.equal(cancelled, true);
});

test('cancellation interrupts a stalled fetch and preserves its reason', async () => {
  const controller = new AbortController();
  const fetch = createSecureLocalFetch({ fetchImpl: () => new Promise(() => {}) });
  const pending = fetch(`${LOCAL_SERVICE_ORIGIN}/health`, { signal: controller.signal });
  controller.abort(Object.assign(new Error('revoked'), { code: 'revoked' }));
  await assert.rejects(pending, { code: 'revoked' });
});

test('request UTF-8 bytes are bounded before calling the companion', async () => {
  let requests = 0;
  const fetch = createSecureLocalFetch({
    maxRequestBytes: 3,
    fetchImpl: async () => { requests += 1; return Response.json({}); },
  });
  await assert.rejects(fetch(`${LOCAL_SERVICE_ORIGIN}/health`, { method: 'POST', body: String.fromCodePoint(0x1f642) }), {
    code: 'local_service_request_too_large',
  });
  assert.equal(requests, 0);
});
