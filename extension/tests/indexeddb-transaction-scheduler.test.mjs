import assert from 'node:assert/strict';
import test from 'node:test';
import { scheduleIndexedDbRequests } from '../transport/indexeddb-transaction-scheduler.mjs';

// Models the native transaction's active request-task window, not its data store.
class NativeTransactionWindow extends EventTarget {
  active = true;
  requests = [];
  objectStore() {
    return { get: () => {
      assert.equal(this.active, true, 'requests may only start inside an active IDB task');
      const request = {};
      this.requests.push(request);
      return request;
    } };
  }
  deliverRequest() {
    const request = this.requests.shift();
    this.active = true;
    try { request.onsuccess(); }
    finally { this.active = false; }
  }
}

test('IDB work returning from an unrelated async task enters a native request callback', async () => {
  const transaction = new NativeTransactionWindow();
  let calls = 0;
  const scheduled = scheduleIndexedDbRequests(transaction, 'synthetic-store', {
    get() { assert.equal(transaction.active, true); calls += 1; return 'synthetic-value'; },
  });
  transaction.active = false;
  await new Promise((resolve) => setTimeout(resolve, 0));
  const pending = scheduled.handle.get();
  assert.equal(calls, 0, 'external async task must not execute an IDB operation');
  transaction.deliverRequest();
  assert.equal(await pending, 'synthetic-value');
  assert.equal(calls, 1);
  scheduled.stop();
  transaction.deliverRequest();
  assert.equal(transaction.requests.length, 0, 'release permits native completion');
  await assert.rejects(scheduled.handle.get(), /no longer active/);
});

test('native transaction abort rejects queued work before it can dispatch', async () => {
  const transaction = new NativeTransactionWindow();
  let calls = 0;
  const scheduled = scheduleIndexedDbRequests(transaction, 'synthetic-store', {
    put() { calls += 1; },
  });
  transaction.active = false;
  const pending = scheduled.handle.put();
  transaction.error = new Error('Synthetic transaction abort');
  transaction.dispatchEvent(new Event('abort'));
  await assert.rejects(pending, transaction.error);
  transaction.deliverRequest();
  assert.equal(calls, 0);
  assert.equal(transaction.requests.length, 0);
  scheduled.stop();
});
