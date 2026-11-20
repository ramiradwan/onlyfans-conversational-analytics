import type { HistorySettings } from '../protocol';
import type { HistorySettingsApi } from './historySettingsApi';
import { integer, literal, nonEmptyString, nullable, object } from '../protocol/validation';

const KEY = 'history-change-recovery-v1';
const baseline = object({
  creator_account_id: nonEmptyString,
  settings_revision: integer(0),
  consent_policy_version: nonEmptyString,
  consent_revision: nullable(nonEmptyString),
  authorized_platform_creator_id: nullable(nonEmptyString),
  recent_window_days: integer(1),
  page_size: integer(1, 100),
  pages_per_wake: integer(1),
  request_interval_ms: integer(0),
  retry_limit: integer(0),
});
const recordShape = object({
  version: literal(1),
  journeyId: nonEmptyString,
  before: baseline,
  desired: literal('running', 'paused', 'revoked'),
  acceptConsent: literal(true, false),
});

export type HistoryBaseline = Pick<HistorySettings, 'creator_account_id' | 'settings_revision'
  | 'consent_policy_version' | 'consent_revision' | 'authorized_platform_creator_id'
  | 'recent_window_days' | 'page_size' | 'pages_per_wake' | 'request_interval_ms' | 'retry_limit'>;
export interface HistoryChange {
  before: HistoryBaseline;
  desired: 'running' | 'paused' | 'revoked';
  acceptConsent: boolean;
}
interface SavedChange extends HistoryChange {
  version: 1;
  journeyId: string;
}
type StorageAccess = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function copyBaseline(value: HistoryBaseline): HistoryBaseline {
  return {
    creator_account_id: value.creator_account_id, settings_revision: value.settings_revision,
    consent_policy_version: value.consent_policy_version, consent_revision: value.consent_revision,
    authorized_platform_creator_id: value.authorized_platform_creator_id,
    recent_window_days: value.recent_window_days, page_size: value.page_size,
    pages_per_wake: value.pages_per_wake, request_interval_ms: value.request_interval_ms,
    retry_limit: value.retry_limit,
  };
}

/** Presentation only. The history owner still admits every write by revision.
 * A saved change is read-only recovery, never a queued mutation. The record
 * contains only the minimum owner metadata required to compare the effect. */
export function createHistoryRecoveryJournal(storage?: () => StorageAccess) {
  let initialized = false;
  let pending: SavedChange | null = null;
  let unavailable = false;
  let restored = false;
  const listeners = new Set<() => void>();
  const getState = () => {
    if (!initialized) {
      initialized = true;
      try {
        const raw = storage?.().getItem(KEY);
        if (raw) {
          const value: unknown = JSON.parse(raw);
          recordShape(value, '$');
          pending = value as SavedChange;
          restored = true;
        }
      } catch { unavailable = true; }
    }
    return pending;
  };
  const publish = (value: SavedChange | null) => {
    try {
      if (value) storage?.().setItem(KEY, JSON.stringify(value));
      else storage?.().removeItem(KEY);
    } catch { unavailable = true; return false; }
    pending = value;
    restored = false;
    for (const listener of listeners) listener();
    return true;
  };
  return {
    getState,
    // Only after a new renderer has read the exact current baseline may the
    // customer make a fresh decision. The owner's revision CAS fences even a
    // delayed original request; this never authorizes automatic resubmission.
    restoredBaseline(value: HistoryChange, current: HistoryBaseline) {
      return restored && getState() === value
        && JSON.stringify(copyBaseline(current)) === JSON.stringify(copyBaseline(value.before));
    },
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    claim(journeyId: string, change: HistoryChange) {
      if (getState() || unavailable || !journeyId) return null;
      const value: SavedChange = { version: 1, journeyId, before: copyBaseline(change.before),
        desired: change.desired, acceptConsent: change.acceptConsent };
      return publish(value) ? value : null;
    },
    clear(value: HistoryChange) {
      if (getState() === value) return publish(null);
      return false;
    },
  };
}

export type HistoryRecoveryJournal = ReturnType<typeof createHistoryRecoveryJournal>;
const nativeJournal = createHistoryRecoveryJournal(() => globalThis.localStorage);
const otherJournals = new WeakMap<HistorySettingsApi, HistoryRecoveryJournal>();
export function historyRecoveryJournal(api: HistorySettingsApi, nativeApi: HistorySettingsApi) {
  if (api === nativeApi) return nativeJournal;
  let journal = otherJournals.get(api);
  if (!journal) { journal = createHistoryRecoveryJournal(); otherJournals.set(api, journal); }
  return journal;
}
