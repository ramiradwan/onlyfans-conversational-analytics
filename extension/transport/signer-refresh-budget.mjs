export const SIGNER_REFRESH_BUDGET_KEY = 'signer-refresh-budget';
export const SIGNER_REFRESH_INTERVAL_MS = 15 * 60_000;
export const SIGNER_REFRESH_WINDOW_MS = 60 * 60_000;
export const SIGNER_REFRESH_LIMIT = 3;

/** Encrypted account-local budget, committed before the browser is allowed to reload. */
export function guardSignerRefresh(chromeApi, { storage, creatorAccountId, signal, now = Date.now }) {
  const tabs = new Proxy(chromeApi.tabs, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver);
      if (property !== 'reload') return typeof value === 'function' ? value.bind(target) : value;
      return async (...args) => {
        signal?.throwIfAborted();
        const time = now();
        await storage.runTransaction('readwrite', ['credentials'], async (tx) => {
          const saved = await tx.get('credentials', SIGNER_REFRESH_BUDGET_KEY);
          if (saved !== undefined && (saved?.key !== SIGNER_REFRESH_BUDGET_KEY
            || saved.creator_account_id !== creatorAccountId || Object.keys(saved).length !== 3
            || !Array.isArray(saved.attempts) || saved.attempts.length > SIGNER_REFRESH_LIMIT
            || saved.attempts.some((stamp) => !Number.isSafeInteger(stamp) || stamp < 0))) {
            throw new Error('Invalid signer refresh budget');
          }
          const attempts = (saved?.attempts ?? []).filter((stamp) => stamp > time - SIGNER_REFRESH_WINDOW_MS).sort((a, b) => a - b);
          const next = Math.max((attempts.at(-1) ?? -SIGNER_REFRESH_INTERVAL_MS) + SIGNER_REFRESH_INTERVAL_MS,
            attempts.length >= SIGNER_REFRESH_LIMIT ? attempts[0] + SIGNER_REFRESH_WINDOW_MS : 0);
          if (time < next) throw Object.assign(new Error('Automatic platform refresh is cooling down'), {
            code: 'history_refresh_backoff', retryAfterMs: next - time,
          });
          signal?.throwIfAborted();
          await tx.put('credentials', { key: SIGNER_REFRESH_BUDGET_KEY,
            creator_account_id: creatorAccountId, attempts: [...attempts, time] });
        });
        // Cancellation after reservation still consumes the attempt: never refund a possible reload.
        signal?.throwIfAborted();
        return value.apply(target, args);
      };
    },
  });
  return new Proxy(chromeApi, {
    get(target, property, receiver) {
      return property === 'tabs' ? tabs : Reflect.get(target, property, receiver);
    },
  });
}
