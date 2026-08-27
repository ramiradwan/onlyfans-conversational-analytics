import { isPreviewObservation } from '../capture/envelopes.mjs';

export const PREVIEW_METRICS_STORAGE_KEY = 'ofca_preview_metrics_v1';
export const PREVIEW_RETENTION_DAYS = 7;

function emptyDay(day) {
  return {
    day,
    chat_observations: 0,
    message_observations: 0,
    inbound_observations: 0,
    outbound_observations: 0,
    unknown_direction_observations: 0,
  };
}

function isCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function validDay(value) {
  return typeof value === 'object'
    && value !== null
    && !Array.isArray(value)
    && Object.keys(value).length === 6
    && /^\d{4}-\d{2}-\d{2}$/.test(value.day)
    && isCount(value.chat_observations)
    && isCount(value.message_observations)
    && isCount(value.inbound_observations)
    && isCount(value.outbound_observations)
    && isCount(value.unknown_direction_observations);
}

function dayNumber(day) {
  return Date.parse(`${day}T00:00:00Z`) / 86_400_000;
}

export function normalizePreviewMetrics(value, now = new Date()) {
  const currentDay = now.toISOString().slice(0, 10);
  const floor = dayNumber(currentDay) - (PREVIEW_RETENTION_DAYS - 1);
  const days = value?.schema === 'ofca-preview-metrics/v1' && Array.isArray(value.days)
    ? value.days.filter((day) => (
      validDay(day) && dayNumber(day.day) >= floor && day.day <= currentDay
    ))
    : [];
  const unique = new Map();
  for (const day of days) unique.set(day.day, structuredClone(day));
  return {
    schema: 'ofca-preview-metrics/v1',
    days: [...unique.values()].sort((left, right) => left.day.localeCompare(right.day)),
  };
}

export function addPreviewObservation(metrics, observation, now = new Date()) {
  if (!isPreviewObservation(observation)) throw new Error('Invalid preview observation');
  const normalized = normalizePreviewMetrics(metrics, now);
  const dayKey = observation.observed_at.slice(0, 10);
  const floor = dayNumber(now.toISOString().slice(0, 10)) - (PREVIEW_RETENTION_DAYS - 1);
  if (dayNumber(dayKey) < floor || dayKey > now.toISOString().slice(0, 10)) return normalized;
  let day = normalized.days.find((candidate) => candidate.day === dayKey);
  if (day === undefined) {
    day = emptyDay(dayKey);
    normalized.days.push(day);
    normalized.days.sort((left, right) => left.day.localeCompare(right.day));
  }
  if (observation.kind === 'chat') {
    day.chat_observations += 1;
  } else {
    day.message_observations += 1;
    if (observation.direction === 'inbound') day.inbound_observations += 1;
    else if (observation.direction === 'outbound') day.outbound_observations += 1;
    else day.unknown_direction_observations += 1;
  }
  return normalizePreviewMetrics(normalized, now);
}

export function summarizePreviewMetrics(metrics, now = new Date()) {
  const normalized = normalizePreviewMetrics(metrics, now);
  const summary = {
    retention_days: PREVIEW_RETENTION_DAYS,
    chat_observations: 0,
    message_observations: 0,
    inbound_observations: 0,
    outbound_observations: 0,
    unknown_direction_observations: 0,
    days: structuredClone(normalized.days),
  };
  for (const day of normalized.days) {
    summary.chat_observations += day.chat_observations;
    summary.message_observations += day.message_observations;
    summary.inbound_observations += day.inbound_observations;
    summary.outbound_observations += day.outbound_observations;
    summary.unknown_direction_observations += day.unknown_direction_observations;
  }
  return summary;
}

export class PreviewMetricsStore {
  constructor({ storage, now = () => new Date() }) {
    if (!storage?.get || !storage?.set || !storage?.remove) {
      throw new Error('Preview metrics require a Chrome storage area');
    }
    this.storage = storage;
    this.now = now;
    this.queue = Promise.resolve();
  }

  async #load() {
    const saved = await this.storage.get([PREVIEW_METRICS_STORAGE_KEY]);
    return normalizePreviewMetrics(saved?.[PREVIEW_METRICS_STORAGE_KEY], this.now());
  }

  record(observation) {
    const operation = this.queue.then(async () => {
      const next = addPreviewObservation(await this.#load(), observation, this.now());
      await this.storage.set({ [PREVIEW_METRICS_STORAGE_KEY]: next });
      return summarizePreviewMetrics(next, this.now());
    });
    this.queue = operation.catch(() => undefined);
    return operation;
  }

  async summary() {
    await this.queue;
    return summarizePreviewMetrics(await this.#load(), this.now());
  }

  async clear() {
    const operation = this.queue.then(() => this.storage.remove([PREVIEW_METRICS_STORAGE_KEY]));
    this.queue = operation.catch(() => undefined);
    await operation;
  }

  async prune() {
    const operation = this.queue.then(async () => {
      const normalized = await this.#load();
      if (normalized.days.length === 0) {
        await this.storage.remove([PREVIEW_METRICS_STORAGE_KEY]);
      } else {
        await this.storage.set({ [PREVIEW_METRICS_STORAGE_KEY]: normalized });
      }
      return summarizePreviewMetrics(normalized, this.now());
    });
    this.queue = operation.catch(() => undefined);
    return operation;
  }
}
