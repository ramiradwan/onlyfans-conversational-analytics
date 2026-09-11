const KEEPALIVE_KEY = '__ofca_native_transaction_keepalive__';

/**
 * WebCrypto completion can run outside an IndexedDB request task. Chromium 132
 * requires new IDB requests to enter during an active request callback, even when
 * another request keeps the transaction alive. Dispatch them from that callback.
 */
export function scheduleIndexedDbRequests(transaction, storeName, handle) {
  if (typeof transaction.addEventListener !== 'function') {
    // The test adapter supplies its own explicit asynchronous transaction hold.
    return { handle, stop() {} };
  }
  let stopped = false;
  const pending = [];
  const rejectPending = (error) => {
    while (pending.length) pending.shift().reject(error);
  };
  const abort = () => {
    stopped = true;
    rejectPending(transaction.error ?? new Error('IndexedDB transaction was aborted'));
  };
  transaction.addEventListener('abort', abort, { once: true });
  const pump = () => {
    if (stopped) return;
    let request;
    try { request = transaction.objectStore(storeName).get(KEEPALIVE_KEY); }
    catch (error) { stopped = true; rejectPending(error); return; }
    request.onerror = () => {
      stopped = true;
      rejectPending(request.error ?? new Error('IndexedDB transaction keepalive failed'));
    };
    request.onsuccess = () => {
      const ready = pending.splice(0);
      for (const item of ready) {
        try { item.resolve(item.operation()); }
        catch (error) { item.reject(error); }
      }
      pump();
    };
  };
  pump();
  return {
    handle: Object.freeze(Object.fromEntries(Object.entries(handle).map(([name, operation]) => [
      name, (...args) => new Promise((resolve, reject) => {
        if (stopped) { reject(new Error('IndexedDB transaction handle is no longer active')); return; }
        pending.push({ resolve, reject, operation: () => operation(...args) });
      }),
    ]))),
    stop() {
      stopped = true;
      transaction.removeEventListener('abort', abort);
      rejectPending(new Error('IndexedDB transaction handle is no longer active'));
    },
  };
}
