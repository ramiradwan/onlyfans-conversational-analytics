import {
  COVERAGE_SOURCE_SEQUENCE_INDEX,
  INGESTION_STORES,
} from './durable-outbox.mjs';

export const INGESTION_DATABASE_NAME_PREFIX = 'onlyfans-agent-account-v2';
export const INGESTION_DATABASE_VERSION = 4;

export async function accountDatabaseName(creatorAccountId, cryptoApi = globalThis.crypto) {
  if (typeof creatorAccountId !== 'string' || creatorAccountId.length === 0) {
    throw new Error('creatorAccountId is required for account-partitioned IndexedDB');
  }
  if (!cryptoApi?.subtle) throw new Error('Web Crypto is required for account partitioning');
  const digest = await cryptoApi.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(creatorAccountId),
  );
  const hash = [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('');
  return `${INGESTION_DATABASE_NAME_PREFIX}-${hash}`;
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'));
  });
}

function openDatabase(indexedDb, databaseName) {
  return new Promise((resolve, reject) => {
    const request = indexedDb.open(databaseName, INGESTION_DATABASE_VERSION);
    let settled = false;

    request.onupgradeneeded = (event) => {
      const database = request.result;
      if (!database.objectStoreNames.contains(INGESTION_STORES.meta)) {
        database.createObjectStore(INGESTION_STORES.meta);
      }
      if (!database.objectStoreNames.contains(INGESTION_STORES.outbox)) {
        database.createObjectStore(INGESTION_STORES.outbox, { keyPath: 'source_seq' });
      }
      if (!database.objectStoreNames.contains(INGESTION_STORES.chats)) {
        database.createObjectStore(INGESTION_STORES.chats, { keyPath: 'chat_id' });
      }
      if (!database.objectStoreNames.contains(INGESTION_STORES.messages)) {
        const messages = database.createObjectStore(
          INGESTION_STORES.messages,
          { keyPath: 'message_id' },
        );
        messages.createIndex('chat_id', 'chat_id', { unique: false });
      }
      let coverageStore;
      if (!database.objectStoreNames.contains(INGESTION_STORES.coverageEvidence)) {
        coverageStore = database.createObjectStore(
          INGESTION_STORES.coverageEvidence,
          { keyPath: 'evidence_key' },
        );
      } else if (event.oldVersion < 3) {
        coverageStore = request.transaction.objectStore(INGESTION_STORES.coverageEvidence);
      }
      if (coverageStore !== undefined) {
        const indexes = coverageStore.indexNames;
        if (indexes === undefined || !indexes.contains(COVERAGE_SOURCE_SEQUENCE_INDEX)) {
          coverageStore.createIndex(
            COVERAGE_SOURCE_SEQUENCE_INDEX,
            'last_source_seq',
            { unique: true },
          );
        }
      }
      const keyedStores = [
        [INGESTION_STORES.historyJobs, 'job_id'],
        [INGESTION_STORES.commandResults, 'key'],
        [INGESTION_STORES.config, 'key'],
        [INGESTION_STORES.snapshotManifests, 'snapshot_id'],
        [INGESTION_STORES.snapshotChunks, 'key'],
        [INGESTION_STORES.snapshotOverrides, 'key'],
        [INGESTION_STORES.credentials, 'key'],
      ];
      for (const [storeName, keyPath] of keyedStores) {
        if (!database.objectStoreNames.contains(storeName)) {
          database.createObjectStore(storeName, { keyPath });
        }
      }
    };
    request.onblocked = () => {
      if (!settled) {
        settled = true;
        reject(new Error(`IndexedDB upgrade for ${databaseName} is blocked`));
      }
    };
    request.onerror = () => {
      if (!settled) {
        settled = true;
        reject(request.error ?? new Error(`Unable to open IndexedDB database ${databaseName}`));
      }
    };
    request.onsuccess = () => {
      const database = request.result;
      database.onversionchange = () => database.close();
      if (settled) {
        database.close();
        return;
      }
      settled = true;
      resolve(database);
    };
  });
}

function transactionCompletion(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(
      transaction.error ?? new Error('IndexedDB transaction was aborted'),
    );
    transaction.onerror = () => {
      // The abort event supplies the final transaction error.
    };
  });
}

function transactionHandle(transaction, storeNames, isActive, keyRangeFactory) {
  const allowedStores = new Set(storeNames);
  const objectStore = (storeName) => {
    if (!isActive()) throw new Error('IndexedDB transaction handle is no longer active');
    if (!allowedStores.has(storeName)) {
      throw new Error(`Object store ${storeName} is outside this transaction`);
    }
    return transaction.objectStore(storeName);
  };

  return Object.freeze({
    get(storeName, key) {
      return requestResult(objectStore(storeName).get(key));
    },
    getAll(storeName) {
      return requestResult(objectStore(storeName).getAll());
    },
    getAllKeys(storeName) {
      return requestResult(objectStore(storeName).getAllKeys());
    },
    getAllKeysFromIndex(storeName, indexName, key) {
      return requestResult(objectStore(storeName).index(indexName).getAllKeys(key));
    },
    getPage(storeName, { afterKey = null, limit = 100 } = {}) {
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 10_000) {
        throw new Error('IndexedDB page limit must be between 1 and 10000');
      }
      const store = objectStore(storeName);
      const range = afterKey === null
        ? undefined
        : keyRangeFactory.lowerBound(afterKey, true);
      return new Promise((resolve, reject) => {
        const rows = [];
        const request = store.openCursor(range);
        request.onerror = () => reject(request.error ?? new Error('IndexedDB cursor failed'));
        request.onsuccess = () => {
          const cursor = request.result;
          if (cursor === null || rows.length >= limit) {
            resolve(rows);
            return;
          }
          rows.push({ key: structuredClone(cursor.primaryKey), value: structuredClone(cursor.value) });
          cursor.continue();
        };
      });
    },
    getPageFromIndex(
      storeName,
      indexName,
      { afterIndexKey = null, limit = 100 } = {},
    ) {
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 10_000) {
        throw new Error('IndexedDB index page limit must be between 1 and 10000');
      }
      const index = objectStore(storeName).index(indexName);
      const range = afterIndexKey === null
        ? undefined
        : keyRangeFactory.lowerBound(afterIndexKey, true);
      return new Promise((resolve, reject) => {
        const rows = [];
        const request = index.openCursor(range);
        request.onerror = () => reject(request.error ?? new Error('IndexedDB index cursor failed'));
        request.onsuccess = () => {
          const cursor = request.result;
          if (cursor === null || rows.length >= limit) {
            resolve(rows);
            return;
          }
          rows.push({
            key: structuredClone(cursor.primaryKey),
            indexKey: structuredClone(cursor.key),
            value: structuredClone(cursor.value),
          });
          cursor.continue();
        };
      });
    },
    put(storeName, value, key = undefined) {
      const store = objectStore(storeName);
      return requestResult(key === undefined ? store.put(value) : store.put(value, key));
    },
    delete(storeName, key) {
      return requestResult(objectStore(storeName).delete(key));
    },
    clear(storeName) {
      return requestResult(objectStore(storeName).clear());
    },
  });
}

/**
 * Creates the MV3 ingestion adapter. Each call opens the database on demand, commits or aborts one
 * native IndexedDB transaction, and closes the connection, so correctness never depends on a
 * service worker retaining a live connection between wakes.
 */
export function createIndexedDbIngestionStorage(
  indexedDb = globalThis.indexedDB,
  {
    creatorAccountId,
    databaseName = null,
    cryptoApi = globalThis.crypto,
    keyRangeFactory = globalThis.IDBKeyRange,
  } = {},
) {
  if (typeof indexedDb?.open !== 'function') throw new Error('IndexedDB is unavailable');
  if (databaseName === null && (typeof creatorAccountId !== 'string' || creatorAccountId.length === 0)) {
    throw new Error('creatorAccountId is required unless an explicit test databaseName is supplied');
  }
  const ranges = keyRangeFactory ?? {
    lowerBound: (value, open = false) => ({ __fake_lower_bound: value, __fake_open: open }),
  };
  const resolvedName = databaseName === null
    ? accountDatabaseName(creatorAccountId, cryptoApi)
    : Promise.resolve(databaseName);

  return Object.freeze({
    databaseName: resolvedName,
    async runTransaction(mode, storeNames, work) {
      if (mode !== 'readonly' && mode !== 'readwrite') {
        throw new Error(`Unsupported IndexedDB transaction mode ${String(mode)}`);
      }
      if (!Array.isArray(storeNames) || storeNames.length === 0) {
        throw new Error('An IndexedDB transaction requires at least one object store');
      }
      if (typeof work !== 'function') throw new Error('IndexedDB transaction work is required');

      const openedName = await resolvedName;
      const database = await openDatabase(indexedDb, openedName);
      let active = true;
      let transaction;
      try {
        transaction = database.transaction([...new Set(storeNames)], mode);
        const completion = transactionCompletion(transaction);
        const handle = transactionHandle(
          transaction,
          storeNames,
          () => active,
          ranges,
        );
        let result;
        try {
          result = await work(handle);
        } catch (error) {
          active = false;
          try {
            transaction.abort();
          } catch {
            // A request failure may already have aborted the transaction.
          }
          await completion.catch(() => undefined);
          throw error;
        }
        active = false;
        await completion;
        return result;
      } finally {
        active = false;
        database.close();
      }
    },
  });
}
