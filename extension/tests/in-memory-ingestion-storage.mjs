import { INGESTION_STORES } from '../transport/durable-outbox.mjs';

const clone = (value) => structuredClone(value);

const PRIMARY_KEYS = Object.freeze({
  [INGESTION_STORES.meta]: null,
  [INGESTION_STORES.outbox]: 'source_seq',
  [INGESTION_STORES.chats]: 'chat_id',
  [INGESTION_STORES.messages]: 'message_id',
  [INGESTION_STORES.coverageEvidence]: 'evidence_key',
  [INGESTION_STORES.historyJobs]: 'job_id',
  [INGESTION_STORES.commandResults]: 'key',
  [INGESTION_STORES.config]: 'key',
  [INGESTION_STORES.snapshotManifests]: 'snapshot_id',
  [INGESTION_STORES.snapshotChunks]: 'key',
  [INGESTION_STORES.snapshotOverrides]: 'key',
  [INGESTION_STORES.credentials]: 'key',
});

function compareKeys(left, right) {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

export class InMemoryIngestionStorage {
  constructor(log = []) {
    this.log = log;
    this.transactions = [];
    this.lastTransactionHandle = null;
    this.failure = null;
    this.chain = Promise.resolve();
    this.stores = new Map(
      Object.values(INGESTION_STORES).map((storeName) => [storeName, new Map()]),
    );
  }

  failNextWriteTransactionAfter(writeCount, error = new Error('Injected transaction failure')) {
    if (!Number.isSafeInteger(writeCount) || writeCount < 1) {
      throw new Error('Injected failure write count must be a positive integer');
    }
    this.failure = { writeCount, error };
  }

  runTransaction(mode, storeNames, work) {
    const run = this.chain.then(() => this.executeTransaction(mode, storeNames, work));
    this.chain = run.then(() => undefined, () => undefined);
    return run;
  }

  async executeTransaction(mode, storeNames, work) {
    if (mode !== 'readonly' && mode !== 'readwrite') {
      throw new Error(`Unsupported in-memory transaction mode ${String(mode)}`);
    }
    if (!Array.isArray(storeNames) || storeNames.length === 0) {
      throw new Error('An in-memory transaction requires at least one object store');
    }
    const selectedStores = [...new Set(storeNames)];
    for (const storeName of selectedStores) {
      if (!this.stores.has(storeName)) throw new Error(`Unknown object store ${storeName}`);
    }

    const transactionRecord = {
      mode,
      storeNames: selectedStores,
      writes: [],
      committed: false,
    };
    this.transactions.push(transactionRecord);
    const workingStores = new Map(selectedStores.map((storeName) => [
      storeName,
      new Map([...this.stores.get(storeName)].map(([key, value]) => [key, clone(value)])),
    ]));
    const injectedFailure = mode === 'readwrite' ? this.failure : null;
    if (injectedFailure !== null) this.failure = null;
    let active = true;

    const store = (storeName) => {
      if (!active) throw new Error('Transaction operations are forbidden outside the transaction');
      if (!workingStores.has(storeName)) {
        throw new Error(`Object store ${storeName} is outside this transaction`);
      }
      return workingStores.get(storeName);
    };
    const ensureWritable = () => {
      if (mode !== 'readwrite') throw new Error('Writes require a readwrite transaction');
    };
    const recordWrite = (write) => {
      transactionRecord.writes.push(clone(write));
      if (injectedFailure?.writeCount === transactionRecord.writes.length) {
        throw injectedFailure.error;
      }
    };
    const primaryKey = (storeName, value, suppliedKey) => {
      if (suppliedKey !== undefined) return suppliedKey;
      const keyPath = PRIMARY_KEYS[storeName];
      const key = keyPath === null ? undefined : value[keyPath];
      if (key === undefined) throw new Error(`A key is required for object store ${storeName}`);
      return key;
    };

    const handle = Object.freeze({
      get(storeName, key) {
        const value = store(storeName).get(key);
        return value === undefined ? undefined : clone(value);
      },
      getAll(storeName) {
        return [...store(storeName)]
          .sort(([left], [right]) => compareKeys(left, right))
          .map(([, value]) => clone(value));
      },
      getAllKeys(storeName) {
        return [...store(storeName).keys()].sort(compareKeys).map(clone);
      },
      getAllKeysFromIndex(storeName, indexName, key) {
        return [...store(storeName)]
          .filter(([, value]) => value[indexName] === key)
          .map(([primary]) => clone(primary))
          .sort(compareKeys);
      },
      getPage(storeName, { afterKey = null, limit = 100 } = {}) {
        return [...store(storeName)]
          .filter(([key]) => afterKey === null || compareKeys(key, afterKey) > 0)
          .sort(([left], [right]) => compareKeys(left, right))
          .slice(0, limit)
          .map(([key, value]) => ({ key: clone(key), value: clone(value) }));
      },
      getPageFromIndex(
        storeName,
        indexName,
        { afterIndexKey = null, limit = 100 } = {},
      ) {
        return [...store(storeName)]
          .filter(([, value]) => (
            afterIndexKey === null || compareKeys(value[indexName], afterIndexKey) > 0
          ))
          .sort(([leftKey, left], [rightKey, right]) => (
            compareKeys(left[indexName], right[indexName]) || compareKeys(leftKey, rightKey)
          ))
          .slice(0, limit)
          .map(([key, value]) => ({
            key: clone(key),
            indexKey: clone(value[indexName]),
            value: clone(value),
          }));
      },
      put(storeName, value, suppliedKey = undefined) {
        ensureWritable();
        const key = primaryKey(storeName, value, suppliedKey);
        store(storeName).set(key, clone(value));
        recordWrite({ operation: 'put', store: storeName, key, value });
      },
      delete(storeName, key) {
        ensureWritable();
        store(storeName).delete(key);
        recordWrite({ operation: 'delete', store: storeName, key });
      },
      clear(storeName) {
        ensureWritable();
        store(storeName).clear();
        recordWrite({ operation: 'clear', store: storeName });
      },
    });
    this.lastTransactionHandle = handle;

    try {
      const result = await work(handle);
      active = false;
      if (mode === 'readwrite') {
        for (const [storeName, values] of workingStores) this.stores.set(storeName, values);
        if (transactionRecord.writes.length > 0) this.log.push('persist');
      }
      transactionRecord.committed = true;
      return result;
    } catch (error) {
      active = false;
      transactionRecord.error = error;
      throw error;
    }
  }
}

export class InMemoryLegacyIngestionStorage {
  constructor(value = null) {
    this.value = value === null ? null : clone(value);
    this.loadCount = 0;
    this.deleteCount = 0;
  }

  async loadLegacyIngestionState() {
    this.loadCount += 1;
    return this.value === null ? null : clone(this.value);
  }

  async deleteLegacyIngestionState() {
    this.deleteCount += 1;
    this.value = null;
  }
}
