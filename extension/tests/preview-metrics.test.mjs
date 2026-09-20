import assert from 'node:assert/strict';
import test from 'node:test';
import { PREVIEW_METRICS_STORAGE_KEY, LEGACY_PREVIEW_METRICS_STORAGE_KEY,
  PreviewMetricsStore, PREVIEW_MAX_ENTRIES } from '../runtime/preview-metrics.mjs';

const NOW = '2030-01-08T12:00:00.000Z';
function observation(id = 'message-synthetic', overrides = {}) {
  return { kind: 'message', creator_id: 'creator-synthetic', chat_id: 'chat-synthetic',
    record_id: id, observed_at: NOW, activity_at: '2030-01-07T11:00:00.000Z', direction: 'inbound', ...overrides };
}
function storageArea() {
  const values = {};
  return { values, async get(keys) { return structuredClone(Object.fromEntries(keys
    .filter((key) => Object.hasOwn(values, key)).map((key) => [key, values[key]]))); },
  async set(update) { Object.assign(values, structuredClone(update)); },
  async remove(keys) { for (const key of keys) delete values[key]; } };
}
function harness() {
  const storage = storageArea(); let clock = new Date(NOW);
  return { storage, setNow: (value) => { clock = new Date(value); },
    reopen: () => new PreviewMetricsStore({ storage, now: () => clock }) };
}

test('reload, pagination overlap, multiple tabs and store restart count each source entity once', async () => {
  const h = harness(); let store = h.reopen();
  await Promise.all([store.record(observation()), store.record(observation()), store.record(observation('second'))]);
  store = h.reopen();
  await store.record(observation());
  await store.record({ kind: 'chat', creator_id: 'creator-synthetic', record_id: 'chat-synthetic',
    observed_at: NOW, activity_at: '2030-01-08T10:00:00Z' });
  const result = await store.summary();
  assert.equal(result.message_observations, 2);
  assert.equal(result.chat_observations, 1);
  assert.equal(result.inbound_observations, 2);
  assert.equal(result.days.find((day) => day.day === '2030-01-07').message_observations, 2);
});

test('activity date, not read date, defines the seven UTC day window', async () => {
  const h = harness(); const store = h.reopen();
  await store.record(observation('expired', { activity_at: '2030-01-01T23:59:59Z' }));
  await store.record(observation('first-day', { activity_at: '2030-01-02T00:00:00Z' }));
  await store.record(observation('future', { activity_at: '2030-01-08T12:01:00Z' }));
  assert.equal((await store.summary()).message_observations, 1);
  h.setNow('2030-01-09T00:00:00Z');
  assert.equal((await store.prune()).message_observations, 0);
  assert.equal(h.storage.values[PREVIEW_METRICS_STORAGE_KEY], undefined);
});

test('persisted state and summary contain no raw IDs, message text, URLs or exact timestamps', async () => {
  const h = harness(); const store = h.reopen(); await store.record(observation());
  const saved = JSON.stringify(h.storage.values);
  for (const value of ['creator-synthetic', 'chat-synthetic', 'message-synthetic', NOW, '11:00:00', 'http']) {
    assert.equal(saved.includes(value), false, value);
  }
  const state = h.storage.values[PREVIEW_METRICS_STORAGE_KEY];
  assert.match(state.key, /^[a-f0-9]{64}$/);
  const summary = JSON.stringify(await store.summary());
  assert.equal(summary.includes(state.key), false);
  assert.equal(summary.includes(state.active_account), false);
  await assert.rejects(store.record({ ...observation(), text: 'forbidden-content' }), /Invalid preview/);
});

test('creator, chat and record kind namespace deduplication; direction correction does not add a message', async () => {
  const h = harness(); const store = h.reopen();
  await store.record(observation('shared', { direction: 'unknown' }));
  await store.record(observation('shared', { direction: 'outbound' }));
  await store.record(observation('shared', { direction: 'unknown' }));
  assert.equal((await store.summary()).outbound_observations, 1);
  await store.record(observation('shared', { chat_id: 'another-chat' }));
  assert.equal((await store.summary()).message_observations, 2);
  await store.record(observation('shared', { creator_id: 'another-creator' }));
  assert.equal((await store.summary()).message_observations, 1);
  await store.record(observation('shared'));
  assert.equal((await store.summary()).message_observations, 2);
});

test('clear removes counts and tokens and rotates the secret on the next observation', async () => {
  const h = harness(); const store = h.reopen(); await store.record(observation());
  const key = h.storage.values[PREVIEW_METRICS_STORAGE_KEY].key;
  await store.clear(); assert.deepEqual(h.storage.values, {});
  await store.record(observation());
  assert.notEqual(h.storage.values[PREVIEW_METRICS_STORAGE_KEY].key, key);
  assert.equal((await store.summary()).message_observations, 1);
});

test('legacy additive counts are discarded rather than relabeled as unique counts', async () => {
  const h = harness(); h.storage.values[LEGACY_PREVIEW_METRICS_STORAGE_KEY] = { days: [{ message_observations: 200 }] };
  assert.equal((await h.reopen().prune()).message_observations, 0);
  assert.deepEqual(h.storage.values, {});
});

test('failed commits and cancelled admission do not poison retry deduplication', async () => {
  const h = harness(); const store = h.reopen(); const set = h.storage.set;
  h.storage.set = async () => { throw new Error('storage unavailable'); };
  await assert.rejects(store.record(observation()), /storage unavailable/);
  h.storage.set = set;
  await assert.rejects(store.record(observation(), { assertCurrent() { throw new Error('stale'); } }), /stale/);
  await store.record(observation()); await store.record(observation());
  assert.equal((await store.summary()).message_observations, 1);
});

test('bounded storage never evicts tokens and silently recounts; saturation is disclosed', async () => {
  const h = harness(); const store = h.reopen(); await store.record(observation());
  const state = h.storage.values[PREVIEW_METRICS_STORAGE_KEY];
  const account = state.accounts[state.active_account];
  for (let index = 0; index < PREVIEW_MAX_ENTRIES - 2; index += 1) {
    account.entries[index.toString(16).padStart(64, '0')] = { day: '2030-01-07', direction: 'inbound' };
  }
  await store.record(observation('new-after-limit'));
  assert.equal((await store.summary()).limited, true);
  assert.equal(Object.keys(h.storage.values[PREVIEW_METRICS_STORAGE_KEY].accounts[state.active_account].entries).length, PREVIEW_MAX_ENTRIES);
});
