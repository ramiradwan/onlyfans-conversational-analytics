import assert from 'node:assert/strict';
import test from 'node:test';
import { guardSignerRefresh, SIGNER_REFRESH_BUDGET_KEY } from '../transport/signer-refresh-budget.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

function harness(storage = new InMemoryIngestionStorage()) {
  let time = 1_800_000_000_000, reloads = 0;
  const chromeApi = { tabs: { async reload() { assert.equal(this, chromeApi.tabs); reloads += 1; } } };
  return { storage, count: () => reloads, advance: (ms) => { time += ms; },
    restart: (signal) => guardSignerRefresh(chromeApi, { storage, creatorAccountId: 'test-account', now: () => time, signal }) };
}

test('refresh reservation is durable, concurrent-safe and capped at one per 15 minutes and three per hour', async () => {
  const h = harness();
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const results = await Promise.allSettled(Array.from({ length: 20 }, () => h.restart().tabs.reload(17)));
    assert.equal(results.filter((r) => r.status === 'fulfilled').length, 1);
    assert.equal(h.count(), attempt + 1);
    await assert.rejects(h.restart().tabs.reload(17), { code: 'history_refresh_backoff' });
    h.advance(15 * 60_000);
  }
  await assert.rejects(h.restart().tabs.reload(17), { code: 'history_refresh_backoff' });
  h.advance(15 * 60_000 - 1);
  await assert.rejects(h.restart().tabs.reload(17), { code: 'history_refresh_backoff' });
  h.advance(1);
  await h.restart().tabs.reload(17);
  assert.equal(h.count(), 4);
});

test('failed persistence cannot reload and cancellation after commit does not refund an attempt', async () => {
  const h = harness();
  h.storage.failNextWriteTransactionAfter(1);
  await assert.rejects(h.restart().tabs.reload(17));
  assert.equal(h.count(), 0);
  const controller = new AbortController();
  const underlying = h.storage.runTransaction.bind(h.storage);
  h.storage.runTransaction = async (...args) => {
    const result = await underlying(...args);
    controller.abort('lost-session-after-reservation');
    return result;
  };
  await assert.rejects(h.restart(controller.signal).tabs.reload(17));
  assert.equal(h.count(), 0);
  h.storage.runTransaction = underlying;
  await assert.rejects(h.restart().tabs.reload(17), { code: 'history_refresh_backoff' });
});

test('foreign or corrupt refresh state fails closed without overwriting evidence', async () => {
  const h = harness();
  const invalid = { key: SIGNER_REFRESH_BUDGET_KEY, creator_account_id: 'other-account', attempts: [] };
  await h.storage.runTransaction('readwrite', ['credentials'], (tx) => tx.put('credentials', invalid));
  await assert.rejects(h.restart().tabs.reload(17));
  assert.equal(h.count(), 0);
  assert.deepEqual(await h.storage.runTransaction('readonly', ['credentials'], (tx) => tx.get('credentials', SIGNER_REFRESH_BUDGET_KEY)), invalid);
});
