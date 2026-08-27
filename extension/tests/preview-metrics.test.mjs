import assert from 'node:assert/strict';
import test from 'node:test';

import {
  PREVIEW_METRICS_STORAGE_KEY,
  PreviewMetricsStore,
  addPreviewObservation,
  normalizePreviewMetrics,
  summarizePreviewMetrics,
} from '../runtime/preview-metrics.mjs';

function observation(kind, observedAt, direction = null) {
  return kind === 'chat'
    ? { kind, observed_at: observedAt }
    : { kind, observed_at: observedAt, direction };
}

function storageArea() {
  const values = {};
  return {
    values,
    async get(keys) {
      return Object.fromEntries(
        keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, structuredClone(values[key])]),
      );
    },
    async set(update) { Object.assign(values, structuredClone(update)); },
    async remove(keys) {
      for (const key of keys) delete values[key];
    },
  };
}

test('preview metrics retain only seven UTC days and never store content', () => {
  const now = new Date('2030-01-08T12:00:00Z');
  let metrics = addPreviewObservation(
    null,
    observation('message', '2030-01-01T23:59:59Z', 'inbound'),
    now,
  );
  metrics = addPreviewObservation(
    metrics,
    observation('chat', '2030-01-02T00:00:00Z'),
    now,
  );
  metrics = addPreviewObservation(
    metrics,
    observation('message', '2030-01-08T11:00:00Z', 'outbound'),
    now,
  );

  const summary = summarizePreviewMetrics(metrics, now);
  assert.equal(summary.retention_days, 7);
  assert.equal(summary.chat_observations, 1);
  assert.equal(summary.message_observations, 1);
  assert.equal(summary.inbound_observations, 0);
  assert.equal(summary.outbound_observations, 1);
  assert.equal(JSON.stringify(metrics).includes('text'), false);
});

test('future-dated preview observations and stored rows are rejected', () => {
  const now = new Date('2030-01-08T12:00:00Z');
  const future = {
    kind: 'message',
    observed_at: '2030-01-09T00:00:00.000Z',
    direction: 'inbound',
  };
  assert.deepEqual(addPreviewObservation({ schema: 'ofca-preview-metrics/v1', days: [] }, future, now), {
    schema: 'ofca-preview-metrics/v1',
    days: [],
  });
  assert.deepEqual(normalizePreviewMetrics({
    schema: 'ofca-preview-metrics/v1',
    days: [{
      day: '2030-01-09',
      chat_observations: 1,
      message_observations: 0,
      inbound_observations: 0,
      outbound_observations: 0,
      unknown_direction_observations: 0,
    }],
  }, now).days, []);
});

test('preview metrics clear removes the complete aggregate record', async () => {
  const storage = storageArea();
  const store = new PreviewMetricsStore({
    storage,
    now: () => new Date('2030-01-08T12:00:00Z'),
  });
  await store.record(observation('message', '2030-01-08T11:00:00Z', 'inbound'));
  assert.equal((await store.summary()).message_observations, 1);
  await store.clear();
  assert.equal(Object.hasOwn(storage.values, PREVIEW_METRICS_STORAGE_KEY), false);
  assert.equal((await store.summary()).message_observations, 0);
});

test('preview pruning physically removes expired aggregate rows without new observations', async () => {
  const storage = storageArea();
  storage.values[PREVIEW_METRICS_STORAGE_KEY] = {
    schema: 'ofca-preview-metrics/v1',
    days: [{
      day: '2029-12-01',
      chat_observations: 1,
      message_observations: 2,
      inbound_observations: 1,
      outbound_observations: 1,
      unknown_direction_observations: 0,
    }],
  };
  const store = new PreviewMetricsStore({
    storage,
    now: () => new Date('2030-01-08T12:00:00Z'),
  });
  assert.equal((await store.prune()).message_observations, 0);
  assert.equal(Object.hasOwn(storage.values, PREVIEW_METRICS_STORAGE_KEY), false);
});
