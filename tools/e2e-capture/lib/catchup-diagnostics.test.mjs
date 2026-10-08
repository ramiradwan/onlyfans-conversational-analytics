import assert from 'node:assert/strict';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { readCatchupMessageIds } from './catchup-diagnostics.mjs';

const response = (status, body) => ({ status, ok: status === 200, json: async () => body });
const conflict = () => response(409, { detail: { code: 'cursor_stale' } });

// Run the exact serialized browser helper with a deterministic deadline clock.
async function execute(fetchResult) {
  let now = 0, timerId = 0, settled = false, result;
  const timers = new Map(), calls = [], deadlines = [];
  const schedule = (callback, delay) => {
    const id = ++timerId;
    timers.set(id, { at: now + delay, callback });
    return id;
  };
  const pending = runInNewContext(`(${readCatchupMessageIds.toString()})('102')`, {
    AbortSignal: { timeout(delay) {
      deadlines.push(delay);
      const controller = new AbortController();
      schedule(() => controller.abort(new DOMException('Timed out', 'TimeoutError')), delay);
      return controller.signal;
    } },
    fetch: async (url, { signal }) => {
      calls.push({ url, signal, at: now });
      return fetchResult(calls.length, signal);
    },
    setTimeout: schedule,
    clearTimeout: (id) => timers.delete(id),
  }).then((value) => { settled = true; result = { value }; }, (error) => { settled = true; result = { error }; });
  for (let turns = 0; turns < 200 && !settled; turns++) {
    await new Promise(setImmediate);
    if (settled) break;
    const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
    assert(next, 'Unbounded wait without a deadline');
    timers.delete(next[0]);
    now = next[1].at;
    next[1].callback();
  }
  assert(settled, 'Helper did not stop within the bounded clock');
  await pending;
  assert.deepEqual(deadlines, [10_000], 'Retries must share the original deadline');
  assert(calls.every(({ signal }) => signal === calls[0].signal));
  assert(calls.every(({ url }) => url === '/api/v1/conversations/102/messages?limit=100'));
  return { ...result, calls, now };
}

test('first-page projection conflicts reconcile without retrying the scenario', async () => {
  const result = await execute((attempt) => attempt <= 3 ? conflict() : response(200, { items: [{ message_id: 'missing102' }] }));
  assert.equal(result.error, undefined);
  assert.deepEqual([...result.value], ['missing102']);
  assert.equal(result.calls.length, 4);
  assert(result.calls.every((call, index) => index === 0 || call.at > result.calls[index - 1].at), 'Retries require awaited backoff');
  assert(result.now < 10_000);
});

test('persistent projection conflicts fail at the original deadline', async () => {
  const result = await execute(conflict);
  assert.match(result.error.message, /status=409 code=cursor_stale attempts=\d+ outcome=deadline$/);
  assert.equal(result.now, 10_000);
  assert(result.calls.length > 2 && result.calls.length <= 100);
});

for (const [status, code] of [[503, 'unavailable'], [409, 'access_denied']]) {
  test(`unrelated ${status}/${code} refusal fails immediately`, async () => {
    const result = await execute(() => response(status, { detail: { code } }));
    assert.match(result.error.message, /attempts=1 outcome=response_rejected$/);
    assert.equal(result.calls.length, 1);
    assert.equal(result.now, 0);
  });
}

test('a hanging retry uses the same deadline and reports its own unavailable response', async () => {
  const result = await execute((attempt, signal) => attempt === 1 ? conflict() : new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  }));
  assert.match(result.error.message, /status=unavailable code=unavailable attempts=2 outcome=deadline$/);
  assert.equal(result.calls.length, 2);
  assert.equal(result.now, 10_000);
});

test('failure diagnostics exclude arbitrary response text', async () => {
  const result = await execute(() => response(500, { detail: 'private response text' }));
  assert.match(result.error.message, /code=unavailable attempts=1 outcome=response_rejected$/);
  assert(!result.error.message.includes('private response text'));
});
