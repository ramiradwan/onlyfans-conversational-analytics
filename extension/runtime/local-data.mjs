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

export async function clearExtensionLocalData({
  chromeApi = globalThis.chrome,
  indexedDb = globalThis.indexedDB,
} = {}) {
  if (!chromeApi?.storage?.local?.clear || !chromeApi?.storage?.session?.clear) {
    throw new Error('Chrome storage clearing is unavailable');
  }
  const databaseNames = await extensionDatabaseNames(indexedDb);
  for (const databaseName of databaseNames) {
    await deleteDatabase(indexedDb, databaseName);
  }
  await chromeApi.storage.local.clear();
  await chromeApi.storage.session.clear();
  return Object.freeze({ deleted_databases: databaseNames.length });
}
