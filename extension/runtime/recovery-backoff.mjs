export const CONNECTION_RECOVERY_KEY = 'companion_recovery_v1';
const SCHEMA = 'ofca-connection-recovery/v1';
export const CONNECTION_STABLE_MS = 60_000;
export const CONNECTION_COOLDOWN_MS = 300_000;
export const CONNECTION_ATTEMPT_LIMIT = 6;

export function connectionRetryDelay(attempt, random = Math.random, baseMs = 1_000, maxMs = 60_000) {
  if (attempt >= CONNECTION_ATTEMPT_LIMIT) return CONNECTION_COOLDOWN_MS;
  const exponential = Math.min(maxMs, baseMs * 2 ** Math.max(0, attempt - 1));
  return Math.min(maxMs, Math.max(baseMs, Math.round(exponential * (0.8 + 0.4 * random()))));
}

/** Reserve before opening a channel. UI polls, alarms and worker restarts share the same budget. */
export function createConnectionRecovery({ storage, now = Date.now, random = Math.random }) {
  let queue = Promise.resolve();
  const serialize = (work) => {
    const operation = queue.then(work);
    queue = operation.catch(() => undefined);
    return operation;
  };
  return Object.freeze({
    reserve() {
      return serialize(async () => {
        const saved = (await storage.get([CONNECTION_RECOVERY_KEY]))[CONNECTION_RECOVERY_KEY];
        if (saved !== undefined && (saved?.schema !== SCHEMA
          || Object.keys(saved).length !== 3 || !Number.isSafeInteger(saved.attempts)
          || saved.attempts < 0 || saved.attempts > CONNECTION_ATTEMPT_LIMIT
          || !Number.isSafeInteger(saved.next_attempt_at) || saved.next_attempt_at < 0)) {
          throw new Error('Invalid connection recovery state');
        }
        const time = now();
        if (saved && time < saved.next_attempt_at) {
          throw Object.assign(new Error('Automatic connection recovery is cooling down'), {
            code: 'companion_recovery_backoff', retryAfterMs: saved.next_attempt_at - time,
          });
        }
        // A completed circuit cooldown admits one new bounded sequence.
        const attempts = (saved?.attempts === CONNECTION_ATTEMPT_LIMIT ? 0 : saved?.attempts ?? 0) + 1;
        await storage.set({ [CONNECTION_RECOVERY_KEY]: { schema: SCHEMA, attempts,
          next_attempt_at: time + connectionRetryDelay(attempts, random) } });
      });
    },
    stable() {
      return serialize(() => storage.set({ [CONNECTION_RECOVERY_KEY]: {
        schema: SCHEMA, attempts: 0, next_attempt_at: 0,
      } }));
    },
  });
}
