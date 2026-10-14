import assert from 'node:assert/strict';
import test from 'node:test';
import { readOnboardingEvents } from './sse.mjs';

function stream() {
  let controller;
  const body = new ReadableStream({ start(value) { controller = value; } });
  const fetch = async () => new Response(body, { headers: {
    'content-type': 'text/event-stream; charset=utf-8', 'X-Onboarding-Capabilities': 'local-onboarding.v1',
  } });
  return { fetch, write: (value) => controller.enqueue(new TextEncoder().encode(value)), close: () => controller.close() };
}

test('fragmented data and heartbeat comments produce only complete onboarding frames', async () => {
  const run = stream(); const received = []; const abort = new AbortController();
  const pending = readOnboardingEvents({ fetch: run.fetch, path: '/api/v1/onboarding/events', signal: abort.signal,
    ready() {}, receive(value) { received.push(value); abort.abort(); } });
  run.write(': keepalive\n\nevent: onboard');
  run.write('ing\r\ndata: {"revision":'); run.write('2}\r\n\r\n');
  await pending;
  assert.deepEqual(received, [{ revision: 2 }]);
});

test('headers and heartbeat do not open snapshot gate before the first subscribed frame is buffered', async () => {
  const run = stream(); const order = []; const abort = new AbortController();
  const pending = readOnboardingEvents({ fetch: run.fetch, path: '/api/v1/onboarding/events', signal: abort.signal,
    ready() { order.push('snapshot may read'); abort.abort(); }, receive() { order.push('buffered'); } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(order, []);
  run.write(': keepalive\n\n');
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(order, []);
  run.write('event: onboarding\ndata: {"revision":0}\n\n');
  await pending;
  assert.deepEqual(order, ['buffered', 'snapshot may read']);
});

test('duplicate fields in wire data are refused before schema validation', async () => {
  const run = stream();
  const pending = readOnboardingEvents({ fetch: run.fetch, path: '/api/v1/onboarding/events', ready() {}, receive() {} });
  run.write('event: onboarding\ndata: {"revision":1,"revision":2}\n\n');
  await assert.rejects(pending, /Duplicate onboarding field/u);
});

test('idle liveness failure ends one subscription without any status requests', async () => {
  const run = stream(); let calls = 0;
  await assert.rejects(readOnboardingEvents({ fetch: async (...args) => { calls++; return run.fetch(...args); },
    path: '/api/v1/provisioning/events', ready() {}, receive() {}, idleTimeoutMs: 5 }), /timed out/u);
  assert.equal(calls, 1);
});

test('unregistered paths and non-negotiated responses fail before delivering data', async () => {
  await assert.rejects(readOnboardingEvents({ fetch: () => { throw new Error('must not fetch'); },
    path: 'https://hosted.invalid/events', ready() {}, receive() {} }), /Unregistered/u);
  await assert.rejects(readOnboardingEvents({ fetch: async () => new Response('{}'),
    path: '/api/v1/onboarding/events', ready() { assert.fail(); }, receive() { assert.fail(); } }), /unavailable/u);
});

test('workspace focus is a distinct bounded nonauthorizing event', async () => {
  const run = stream(); const abort = new AbortController(); const focused = [];
  const id = '11111111-1111-4111-8111-111111111111';
  const pending = readOnboardingEvents({ fetch: run.fetch, path: '/api/v1/provisioning/events', signal: abort.signal,
    ready() {}, receive() { assert.fail('focus is not state'); },
    onFocus(value) { focused.push(value); abort.abort(); } });
  run.write(`event: workspace-focus\ndata: {"journey_id":"${id}"}\n\n`);
  await pending; assert.deepEqual(focused, [id]);
});

test('workspace focus cannot carry a navigation destination or authority', async () => {
  const run = stream();
  const pending = readOnboardingEvents({ fetch: run.fetch, path: '/api/v1/provisioning/events',
    ready() {}, receive() {}, onFocus() { assert.fail(); } });
  run.write('event: workspace-focus\ndata: {"journey_id":"11111111-1111-4111-8111-111111111111","url":"https://other.invalid"}\n\n');
  await assert.rejects(pending, /Invalid workspace focus/u);
});
