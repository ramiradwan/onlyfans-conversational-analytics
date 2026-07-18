import { INGESTION_STORES } from './durable-outbox.mjs';

export const INGESTION_DATABASE_NAME = 'onlyfans-agent-durable-ingestion';
export const INGESTION_DATABASE_VERSION = 1;

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

    request.onupgradeneeded = () => {
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
      if (!database.objectStoreNames.contains(INGESTION_STORES.snapshot)) {
        database.createObjectStore(INGESTION_STORES.snapshot, { keyPath: 'key' });
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

function transactionHandle(transaction, storeNames, isActive) {
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
  { databaseName = INGESTION_DATABASE_NAME } = {},
) {
  if (typeof indexedDb?.open !== 'function') throw new Error('IndexedDB is unavailable');

  return Object.freeze({
    async runTransaction(mode, storeNames, work) {
      if (mode !== 'readonly' && mode !== 'readwrite') {
        throw new Error(`Unsupported IndexedDB transaction mode ${String(mode)}`);
      }
      if (!Array.isArray(storeNames) || storeNames.length === 0) {
        throw new Error('An IndexedDB transaction requires at least one object store');
      }
      if (typeof work !== 'function') throw new Error('IndexedDB transaction work is required');

      const database = await openDatabase(indexedDb, databaseName);
      let active = true;
      let transaction;
      try {
        transaction = database.transaction([...new Set(storeNames)], mode);
        const completion = transactionCompletion(transaction);
        const handle = transactionHandle(transaction, storeNames, () => active);
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
