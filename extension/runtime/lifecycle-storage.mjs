export function createLifecycleStorage(storage, signal) {
  if (!storage?.runTransaction) throw new TypeError('Lifecycle storage requires transaction storage');
  if (!signal || typeof signal.throwIfAborted !== 'function') {
    throw new TypeError('Lifecycle storage requires an AbortSignal');
  }

  const pending = new Set();
  return Object.freeze({
    ...storage,
    databaseName: storage.databaseName,
    drain: () => Promise.allSettled([...pending]),
    runTransaction(mode, storeNames, work, controls = {}) {
      const operation = (async () => {
        const combinedSignal = controls.signal && controls.signal !== signal
          ? AbortSignal.any([signal, controls.signal]) : signal;
        combinedSignal.throwIfAborted();
        controls.assertCurrent?.();
        return storage.runTransaction(mode, storeNames, async (tx) => {
          combinedSignal.throwIfAborted();
          controls.assertCurrent?.();
          const result = await work(tx);
          combinedSignal.throwIfAborted();
          controls.assertCurrent?.();
          return result;
        }, { ...controls, signal: combinedSignal });
      })();
      pending.add(operation);
      const remove = () => pending.delete(operation);
      void operation.then(remove, remove);
      return operation;
    },
  });
}
