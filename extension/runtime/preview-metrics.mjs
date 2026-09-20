import { isPreviewObservation } from '../capture/envelopes.mjs';

export const PREVIEW_METRICS_STORAGE_KEY = 'ofca_preview_metrics_v2';
export const LEGACY_PREVIEW_METRICS_STORAGE_KEY = 'ofca_preview_metrics_v1';
export const PREVIEW_RETENTION_DAYS = 7;
export const PREVIEW_MAX_ENTRIES = 20_000;
const HEX = /^[a-f0-9]{64}$/;
const SCHEMA = 'ofca-preview-metrics/v2';
const DAY_MS = 86_400_000;
const encoder = new TextEncoder();
const hex = (bytes) => [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
const unhex = (value) => Uint8Array.from(value.match(/../g), (byte) => Number.parseInt(byte, 16));
const dayNumber = (day) => Date.parse(`${day}T00:00:00Z`) / DAY_MS;

function emptyState() {
  return { schema: SCHEMA, key: hex(crypto.getRandomValues(new Uint8Array(32))), active_account: null, accounts: {} };
}

function withinWindow(day, now) {
  if (typeof day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  const today = Math.floor(now.getTime() / DAY_MS);
  const number = dayNumber(day);
  return number >= today - (PREVIEW_RETENTION_DAYS - 1) && number <= today;
}

export function normalizePreviewMetrics(value, now = new Date()) {
  if (value?.schema !== SCHEMA || !HEX.test(value.key ?? '')) return null;
  const result = { schema: SCHEMA, key: value.key, active_account: null, accounts: {} };
  let count = 0;
  for (const [accountToken, account] of Object.entries(value.accounts ?? {})) {
    if (!HEX.test(accountToken)) continue;
    const entries = {};
    for (const [token, entry] of Object.entries(account?.entries ?? {})) {
      if (!HEX.test(token) || !withinWindow(entry?.day, now)
        || !['chat', 'inbound', 'outbound', 'unknown'].includes(entry?.direction)) continue;
      if (count >= PREVIEW_MAX_ENTRIES) break;
      entries[token] = { day: entry.day, direction: entry.direction };
      count += 1;
    }
    if (Object.keys(entries).length > 0) result.accounts[accountToken] = {
      entries, limited: account.limited === true,
    };
  }
  if (HEX.test(value.active_account ?? '')) result.active_account = value.active_account;
  return result;
}

export function summarizePreviewMetrics(metrics, now = new Date()) {
  const normalized = normalizePreviewMetrics(metrics, now);
  const account = normalized?.accounts[normalized.active_account];
  const summary = { retention_days: PREVIEW_RETENTION_DAYS, chat_observations: 0,
    message_observations: 0, inbound_observations: 0, outbound_observations: 0,
    unknown_direction_observations: 0, limited: account?.limited === true, days: [] };
  const days = new Map();
  for (const entry of Object.values(account?.entries ?? {})) {
    if (!days.has(entry.day)) days.set(entry.day, { day: entry.day, chat_observations: 0,
      message_observations: 0, inbound_observations: 0, outbound_observations: 0,
      unknown_direction_observations: 0 });
    const day = days.get(entry.day);
    const field = entry.direction === 'chat' ? 'chat_observations' : 'message_observations';
    day[field] += 1; summary[field] += 1;
    if (entry.direction !== 'chat') {
      const direction = entry.direction === 'unknown' ? 'unknown_direction_observations' : `${entry.direction}_observations`;
      day[direction] += 1; summary[direction] += 1;
    }
  }
  summary.days = [...days.values()].sort((a, b) => a.day.localeCompare(b.day));
  return summary;
}

async function token(key, ...parts) {
  return hex(new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(JSON.stringify(parts)))));
}

export class PreviewMetricsStore {
  constructor({ storage, now = () => new Date() }) {
    if (!storage?.get || !storage?.set || !storage?.remove) throw new Error('Preview metrics require a Chrome storage area');
    this.storage = storage;
    this.now = now;
    this.queue = Promise.resolve();
  }

  async #load() {
    await this.storage.setAccessLevel?.({ accessLevel: 'TRUSTED_CONTEXTS' });
    const saved = await this.storage.get([PREVIEW_METRICS_STORAGE_KEY]);
    return normalizePreviewMetrics(saved?.[PREVIEW_METRICS_STORAGE_KEY], this.now());
  }

  record(observation, { assertCurrent = () => {} } = {}) {
    // Nothing from the source record is ever included in an error or persisted.
    if (!isPreviewObservation(observation)) return Promise.reject(new Error('Invalid preview observation'));
    const operation = this.queue.then(async () => {
      assertCurrent();
      const now = this.now();
      const next = await this.#load() ?? emptyState();
      const key = await crypto.subtle.importKey('raw', unhex(next.key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
      const accountToken = await token(key, 'account', observation.creator_id);
      next.active_account = accountToken;
      const day = new Date(observation.activity_at).toISOString().slice(0, 10);
      if (withinWindow(day, now) && Date.parse(observation.activity_at) <= now.getTime()) {
        const account = next.accounts[accountToken] ?? { entries: {}, limited: false };
        next.accounts[accountToken] = account;
        let count = Object.values(next.accounts).reduce((sum, item) => sum + Object.keys(item.entries).length, 0);
        const chatId = observation.kind === 'chat' ? observation.record_id : observation.chat_id;
        const chatToken = await token(key, 'chat', observation.creator_id, chatId);
        const changes = [[chatToken, 'chat']];
        if (observation.kind === 'message') changes.push([
          await token(key, 'message', observation.creator_id, chatId, observation.record_id), observation.direction,
        ]);
        for (const [entryToken, direction] of changes) {
          const previous = account.entries[entryToken];
          if (!previous && count >= PREVIEW_MAX_ENTRIES) { account.limited = true; continue; }
          if (!previous) count += 1;
          account.entries[entryToken] = {
            day: direction === 'chat' && previous?.day > day ? previous.day : day,
            direction: direction === 'unknown' && previous ? previous.direction : direction,
          };
        }
      }
      assertCurrent();
      // The key, tokens, dates and directions commit together, including after a worker restart.
      await this.storage.set({ [PREVIEW_METRICS_STORAGE_KEY]: next });
      return summarizePreviewMetrics(next, now);
    });
    this.queue = operation.catch(() => undefined);
    return operation;
  }

  async drain() { await this.queue; }
  async summary() { await this.queue; return summarizePreviewMetrics(await this.#load(), this.now()); }

  async clear() {
    const operation = this.queue.then(() => this.storage.remove([
      PREVIEW_METRICS_STORAGE_KEY, LEGACY_PREVIEW_METRICS_STORAGE_KEY,
    ]));
    this.queue = operation.catch(() => undefined);
    await operation;
  }

  async prune() {
    const operation = this.queue.then(async () => {
      const normalized = await this.#load();
      // Observation totals cannot be migrated into unique activity counts.
      await this.storage.remove([LEGACY_PREVIEW_METRICS_STORAGE_KEY]);
      if (normalized === null || Object.keys(normalized.accounts).length === 0) {
        await this.storage.remove([PREVIEW_METRICS_STORAGE_KEY]);
      } else await this.storage.set({ [PREVIEW_METRICS_STORAGE_KEY]: normalized });
      return summarizePreviewMetrics(normalized, this.now());
    });
    this.queue = operation.catch(() => undefined);
    return operation;
  }
}
