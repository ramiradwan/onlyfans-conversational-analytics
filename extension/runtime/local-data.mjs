import { DELETE_INTENT_KEY } from './deletion-state.mjs';

function deleteDatabase(indexedDb, databaseName) {
  return new Promise((resolve, reject) => {
    const request = indexedDb.deleteDatabase(databaseName);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error ?? new Error('IndexedDB deletion failed'));
    request.onblocked = () => reject(new Error('IndexedDB deletion was blocked'));
  });
}

export async function extensionDatabaseNames(indexedDb = globalThis.indexedDB) {
  if (typeof indexedDb?.databases !== 'function') {
    throw new Error('IndexedDB database enumeration is unavailable');
  }
  const databases = await indexedDb.databases();
  return databases
    .map((entry) => entry?.name)
    .filter((name) => typeof name === 'string' && name.length > 0);
}

async function clearLocalStoragePreservingIntent(storage) {
  const stored = await storage.get(null);
  const keys = Object.keys(stored ?? {}).filter((key) => key !== DELETE_INTENT_KEY);
  if (keys.length > 0) await storage.remove(keys);
}

export async function clearExtensionLocalData({
  chromeApi = globalThis.chrome,
  indexedDb = globalThis.indexedDB,
} = {}) {
  if (
    !chromeApi?.storage?.local?.get
    || !chromeApi?.storage?.local?.remove
    || !chromeApi?.storage?.session?.clear
  ) {
    throw new Error('Chrome storage clearing is unavailable');
  }

  const failures = [];
  let databaseNames = [];
  try {
    databaseNames = await extensionDatabaseNames(indexedDb);
  } catch (error) {
    failures.push({ stage: 'database_enumeration', error });
  }

  for (const databaseName of databaseNames) {
    try {
      await deleteDatabase(indexedDb, databaseName);
    } catch (error) {
      failures.push({ stage: 'database_delete', database_name: databaseName, error });
    }
  }

  try {
    await clearLocalStoragePreservingIntent(chromeApi.storage.local);
  } catch (error) {
    failures.push({ stage: 'local_storage_clear', error });
  }

  try {
    await chromeApi.storage.session.clear();
  } catch (error) {
    failures.push({ stage: 'session_storage_clear', error });
  }

  if (failures.length > 0) {
    const error = new Error('Extension local-data deletion is incomplete');
    error.code = 'delete_incomplete';
    error.failures = failures.map((failure) => ({
      stage: failure.stage,
      ...(failure.database_name === undefined ? {} : { database_name: failure.database_name }),
    }));
    throw error;
  }

  return Object.freeze({ deleted_databases: databaseNames.length });
}
