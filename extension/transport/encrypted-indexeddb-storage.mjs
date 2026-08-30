const FORMAT = 'ofca-idb-aesgcm/v1';
export const ENCRYPTION_KEY_CHECK_STORE = '__ofca_encryption_key_check';
const ENCRYPTION_INFO = new TextEncoder().encode('ofca-extension-indexeddb-encryption-v1');
const INDEX_INFO = new TextEncoder().encode('ofca-extension-indexeddb-index-v1');
const HKDF_SALT = new TextEncoder().encode('ofca-extension-indexeddb-hkdf-v1');
const KEEPALIVE_KEY = '__ofca_encrypted_transaction_keepalive__';

function compareKeys(left, right) {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonical(value[key])]),
    );
  }
  return value;
}

function canonicalJson(value) {
  return JSON.stringify(canonical(value));
}

function bytesToBase64(bytes) {
  let encoded = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    encoded += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(encoded);
}

function base64ToBytes(value) {
  if (typeof value !== 'string' || value.length === 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
    throw new Error('Full-mode encryption key is invalid');
  }
  let decoded;
  try {
    decoded = atob(value);
  } catch (_error) {
    throw new Error('Full-mode encryption key is invalid');
  }
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

function keyBytes(value) {
  if (typeof value === 'string') return base64ToBytes(value);
  if (value instanceof Uint8Array) return new Uint8Array(value);
  if (value instanceof ArrayBuffer) return new Uint8Array(value.slice(0));
  throw new Error('Full-mode encryption key is required');
}

function validatedSpec(storeSpecs, storeName) {
  const spec = storeSpecs[storeName];
  if (!spec) throw new Error(`Encrypted IndexedDB store ${storeName} is not declared`);
  return spec;
}

function logicalPrimary(spec, value, suppliedKey) {
  const key = spec.primaryField === null ? suppliedKey : value?.[spec.primaryField];
  if (key === undefined || key === null) throw new Error('Encrypted IndexedDB record key is required');
  return key;
}

function routeFields(spec) {
  return [
    ...(spec.primaryField === null ? [] : [spec.primaryField]),
    ...Object.keys(spec.indexes),
  ];
}

function routeDocument(storeName, rawPrimary, rawIndexes) {
  return {
    format: FORMAT,
    store: storeName,
    primary_key: rawPrimary,
    indexes: rawIndexes,
  };
}

async function deriveKeys(encryptionKey, databaseName, cryptoApi) {
  if (!cryptoApi?.subtle || typeof cryptoApi.getRandomValues !== 'function') {
    throw new Error('Web Crypto is required for Full-mode encrypted storage');
  }
  const source = keyBytes(encryptionKey);
  if (source.byteLength !== 32) {
    source.fill(0);
    throw new Error('Full-mode encryption key must contain 32 bytes');
  }
  try {
    const root = await cryptoApi.subtle.importKey('raw', source, 'HKDF', false, ['deriveKey']);
    const databaseBinding = new TextEncoder().encode(databaseName);
    const [encryption, index] = await Promise.all([
      cryptoApi.subtle.deriveKey(
        {
          name: 'HKDF',
          hash: 'SHA-256',
          salt: HKDF_SALT,
          info: new Uint8Array([...ENCRYPTION_INFO, 0, ...databaseBinding]),
        },
        root,
        { name: 'AES-GCM', length: 256 },
        false,
        ['encrypt', 'decrypt'],
      ),
      cryptoApi.subtle.deriveKey(
        {
          name: 'HKDF',
          hash: 'SHA-256',
          salt: HKDF_SALT,
          info: new Uint8Array([...INDEX_INFO, 0, ...databaseBinding]),
        },
        root,
        { name: 'HMAC', hash: 'SHA-256', length: 256 },
        false,
        ['sign'],
      ),
    ]);
    return { encryption, index };
  } finally {
    source.fill(0);
  }
}

async function tokenFor(key, protection, keys, cryptoApi) {
  if (protection === 'clear') {
    if (!Number.isSafeInteger(key) || key < 0) {
      throw new Error('Clear IndexedDB routing keys must be non-negative safe integers');
    }
    return key;
  }
  if (protection !== 'hmac' || (typeof key !== 'string' && typeof key !== 'number')) {
    throw new Error('Encrypted IndexedDB routing key is invalid');
  }
  const material = new TextEncoder().encode(canonicalJson([typeof key, key]));
  const signature = new Uint8Array(await cryptoApi.subtle.sign('HMAC', keys.index, material));
  return `h1:${[...signature].map((byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

async function encryptedRecord({
  storeName,
  value,
  suppliedKey,
  spec,
  keys,
  cryptoApi,
}) {
  const cloned = structuredClone(value);
  const primary = logicalPrimary(spec, cloned, suppliedKey);
  const rawPrimary = await tokenFor(primary, spec.primaryProtection, keys, cryptoApi);
  const rawIndexes = {};
  for (const [field, protection] of Object.entries(spec.indexes)) {
    rawIndexes[field] = await tokenFor(cloned?.[field], protection, keys, cryptoApi);
  }
  const route = routeDocument(storeName, rawPrimary, rawIndexes);
  const plaintext = new TextEncoder().encode(canonicalJson({ key: primary, value: cloned }));
  const nonce = cryptoApi.getRandomValues(new Uint8Array(12));
  const ciphertext = new Uint8Array(await cryptoApi.subtle.encrypt(
    {
      name: 'AES-GCM',
      iv: nonce,
      additionalData: new TextEncoder().encode(canonicalJson(route)),
      tagLength: 128,
    },
    keys.encryption,
    plaintext,
  ));
  const envelope = {
    __ofca_format: FORMAT,
    __ofca_nonce: bytesToBase64(nonce),
    __ofca_ciphertext: bytesToBase64(ciphertext),
  };
  if (spec.primaryField !== null) envelope[spec.primaryField] = rawPrimary;
  for (const [field, rawIndex] of Object.entries(rawIndexes)) envelope[field] = rawIndex;
  return { envelope, primary, rawPrimary };
}

async function decryptedRecord({
  storeName,
  envelope,
  rawPrimary,
  spec,
  keys,
  cryptoApi,
}) {
  try {
    if (typeof envelope !== 'object' || envelope === null || Array.isArray(envelope)) {
      throw new Error('envelope');
    }
    const expectedFields = [
      '__ofca_format',
      '__ofca_nonce',
      '__ofca_ciphertext',
      ...routeFields(spec),
    ].sort();
    if (
      envelope.__ofca_format !== FORMAT
      || canonicalJson(Object.keys(envelope).sort()) !== canonicalJson(expectedFields)
    ) throw new Error('format');
    const routedPrimary = spec.primaryField === null ? rawPrimary : envelope[spec.primaryField];
    if (canonicalJson(routedPrimary) !== canonicalJson(rawPrimary)) throw new Error('primary');
    const rawIndexes = Object.fromEntries(
      Object.keys(spec.indexes).map((field) => [field, envelope[field]]),
    );
    const route = routeDocument(storeName, rawPrimary, rawIndexes);
    const nonce = base64ToBytes(envelope.__ofca_nonce);
    if (nonce.byteLength !== 12) throw new Error('nonce');
    const plaintext = await cryptoApi.subtle.decrypt(
      {
        name: 'AES-GCM',
        iv: nonce,
        additionalData: new TextEncoder().encode(canonicalJson(route)),
        tagLength: 128,
      },
      keys.encryption,
      base64ToBytes(envelope.__ofca_ciphertext),
    );
    const decoded = JSON.parse(new TextDecoder().decode(plaintext));
    if (
      typeof decoded !== 'object'
      || decoded === null
      || Array.isArray(decoded)
      || canonicalJson(Object.keys(decoded).sort()) !== canonicalJson(['key', 'value'])
    ) throw new Error('plaintext');
    const recalculated = await encryptedRouteOnly(
      storeName,
      decoded.value,
      decoded.key,
      spec,
      keys,
      cryptoApi,
    );
    if (canonicalJson(recalculated) !== canonicalJson(route)) throw new Error('route');
    return { key: decoded.key, value: structuredClone(decoded.value) };
  } catch (_error) {
    throw new Error('Full-mode encrypted storage could not be authenticated');
  }
}

async function encryptedRouteOnly(storeName, value, primary, spec, keys, cryptoApi) {
  const expectedPrimary = logicalPrimary(spec, value, spec.primaryField === null ? primary : undefined);
  if (canonicalJson(expectedPrimary) !== canonicalJson(primary)) throw new Error('primary');
  const rawPrimary = await tokenFor(primary, spec.primaryProtection, keys, cryptoApi);
  const rawIndexes = {};
  for (const [field, protection] of Object.entries(spec.indexes)) {
    rawIndexes[field] = await tokenFor(value?.[field], protection, keys, cryptoApi);
  }
  return routeDocument(storeName, rawPrimary, rawIndexes);
}

function transactionHandle(raw, storeSpecs, keys, cryptoApi) {
  const decrypt = async (storeName, rawPrimary, envelope) => decryptedRecord({
    storeName,
    envelope,
    rawPrimary,
    spec: validatedSpec(storeSpecs, storeName),
    keys,
    cryptoApi,
  });
  return Object.freeze({
    async get(storeName, key) {
      const spec = validatedSpec(storeSpecs, storeName);
      const rawKey = await tokenFor(key, spec.primaryProtection, keys, cryptoApi);
      const envelope = await raw.get(storeName, rawKey);
      return envelope === undefined ? undefined : (await decrypt(storeName, rawKey, envelope)).value;
    },
    async getAll(storeName) {
      const rawKeys = await raw.getAllKeys(storeName);
      const envelopes = await Promise.all(rawKeys.map((key) => raw.get(storeName, key)));
      const records = await Promise.all(
        rawKeys.map((key, index) => decrypt(storeName, key, envelopes[index])),
      );
      return records.sort((left, right) => compareKeys(left.key, right.key)).map((row) => row.value);
    },
    async getAllKeys(storeName) {
      const rawKeys = await raw.getAllKeys(storeName);
      const envelopes = await Promise.all(rawKeys.map((key) => raw.get(storeName, key)));
      const records = await Promise.all(
        rawKeys.map((key, index) => decrypt(storeName, key, envelopes[index])),
      );
      return records.map((row) => row.key).sort(compareKeys);
    },
    async getAllKeysFromIndex(storeName, indexName, key) {
      const spec = validatedSpec(storeSpecs, storeName);
      const protection = spec.indexes[indexName];
      if (protection === undefined) throw new Error(`Encrypted IndexedDB index ${indexName} is not declared`);
      const rawIndexKey = await tokenFor(key, protection, keys, cryptoApi);
      const rawKeys = await raw.getAllKeysFromIndex(storeName, indexName, rawIndexKey);
      const envelopes = await Promise.all(rawKeys.map((rawKey) => raw.get(storeName, rawKey)));
      const records = await Promise.all(
        rawKeys.map((rawKey, index) => decrypt(storeName, rawKey, envelopes[index])),
      );
      return records.map((row) => row.key).sort(compareKeys);
    },
    async getPage(storeName, options = {}) {
      const spec = validatedSpec(storeSpecs, storeName);
      const afterKey = options.afterKey ?? null;
      const rawAfterKey = afterKey === null
        ? null
        : await tokenFor(afterKey, spec.primaryProtection, keys, cryptoApi);
      const rows = await raw.getPage(storeName, { ...options, afterKey: rawAfterKey });
      const decrypted = await Promise.all(
        rows.map((row) => decrypt(storeName, row.key, row.value)),
      );
      return decrypted.map((row) => ({ key: row.key, value: row.value }));
    },
    async getPageFromIndex(storeName, indexName, options = {}) {
      const spec = validatedSpec(storeSpecs, storeName);
      const protection = spec.indexes[indexName];
      if (protection === undefined) throw new Error(`Encrypted IndexedDB index ${indexName} is not declared`);
      const afterIndexKey = options.afterIndexKey ?? null;
      const rawAfterIndexKey = afterIndexKey === null
        ? null
        : await tokenFor(afterIndexKey, protection, keys, cryptoApi);
      const rows = await raw.getPageFromIndex(
        storeName,
        indexName,
        { ...options, afterIndexKey: rawAfterIndexKey },
      );
      const decrypted = await Promise.all(
        rows.map((row) => decrypt(storeName, row.key, row.value)),
      );
      return decrypted.map((row) => ({
        key: row.key,
        indexKey: row.value[indexName],
        value: row.value,
      }));
    },
    async put(storeName, value, key = undefined) {
      const spec = validatedSpec(storeSpecs, storeName);
      if (spec.primaryField !== null && key !== undefined) {
        throw new Error('A supplied key is invalid for a key-path IndexedDB store');
      }
      const encrypted = await encryptedRecord({
        storeName,
        value,
        suppliedKey: key,
        spec,
        keys,
        cryptoApi,
      });
      if (spec.primaryField === null) await raw.put(storeName, encrypted.envelope, encrypted.rawPrimary);
      else await raw.put(storeName, encrypted.envelope);
      return encrypted.primary;
    },
    async delete(storeName, key) {
      const spec = validatedSpec(storeSpecs, storeName);
      const rawKey = await tokenFor(key, spec.primaryProtection, keys, cryptoApi);
      return raw.delete(storeName, rawKey);
    },
    clear(storeName) {
      validatedSpec(storeSpecs, storeName);
      return raw.clear(storeName);
    },
  });
}

async function keepTransactionAlive(raw, storeName, active) {
  while (active()) {
    await raw.get(storeName, KEEPALIVE_KEY);
  }
}

async function assertPartitionKey(raw, mode, databaseName, keys, cryptoApi) {
  const material = new TextEncoder().encode(`ofca-key-check-v1:${databaseName}`);
  const signature = new Uint8Array(await cryptoApi.subtle.sign('HMAC', keys.index, material));
  const expected = {
    format: 'ofca-idb-key-check/v1',
    check: bytesToBase64(signature),
  };
  const saved = await raw.get(ENCRYPTION_KEY_CHECK_STORE, 'key');
  if (saved === undefined) {
    if (mode !== 'readwrite') {
      throw new Error('Full-mode encrypted storage has not been initialized');
    }
    await raw.put(ENCRYPTION_KEY_CHECK_STORE, expected, 'key');
    return;
  }
  if (canonicalJson(saved) !== canonicalJson(expected)) {
    throw new Error('Full-mode encryption key does not match this account partition');
  }
}

export function createEncryptedIndexedDbStorage(rawStorage, {
  encryptionKey,
  databaseName,
  storeSpecs,
  cryptoApi = globalThis.crypto,
  transactionKeepAlive = globalThis.IDBKeyRange !== undefined,
}) {
  if (!rawStorage?.runTransaction) throw new Error('Raw IndexedDB storage is required');
  const resolvedName = Promise.resolve(databaseName);
  const keys = resolvedName.then((name) => deriveKeys(encryptionKey, name, cryptoApi));
  return Object.freeze({
    databaseName: resolvedName,
    async runTransaction(mode, storeNames, work) {
      const derived = await keys;
      const openedName = await resolvedName;
      return rawStorage.runTransaction(mode, storeNames, async (raw) => {
        let active = true;
        const keepalive = transactionKeepAlive
          ? keepTransactionAlive(raw, storeNames[0], () => active)
          : null;
        try {
          await assertPartitionKey(raw, mode, openedName, derived, cryptoApi);
          return await work(transactionHandle(raw, storeSpecs, derived, cryptoApi));
        } finally {
          active = false;
          await keepalive;
        }
      });
    },
  });
}
