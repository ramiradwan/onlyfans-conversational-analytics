/** Shared device-bound persistence adapter for the full and read-only graphs. */
import { createSecureLocalFetch } from './secure-local-fetch.mjs';
import {
  LOCAL_SERVICE_ORIGIN,
  LOCAL_SERVICE_STORAGE_UNSEAL,
  LOCAL_SERVICE_STORAGE_ROTATE,
  assertLocalServiceUrl,
} from './local-service-endpoints.mjs';

const INSTALLATION_ID_KEY = 'agent_installation_id';
export const ACTIVE_ACCOUNT_PARTITION_KEY = 'active_account_partition_v5';
export const FULL_STORAGE_BOOTSTRAP_KEY = 'ofca_full_storage_bootstrap_v1';
export const BRAIN_BINDING_MESSAGE_TYPE = 'ofca.agent.bind';
export const RECONCILE_ALARM_NAME = 'ofca-agent-reconcile';

const GLOBAL_CREDENTIAL_KEYS_TO_REMOVE = Object.freeze([
  'brain_binding_v2',
  'agent_reconnect_auth_v2',
  'active_account_partition_v4',
]);
const UNLOCK_SCHEMA = 'ofca-extension-storage-unlock/v1';
const ROTATION_SCHEMA = 'ofca-extension-storage-rotation/v1';

function storageGet(storage, keys) {
  return new Promise((resolve, reject) => {
    storage.get(keys, (value) => {
      const error = globalThis.chrome?.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve(value ?? {});
    });
  });
}

function storageSet(storage, value) {
  return new Promise((resolve, reject) => {
    storage.set(value, () => {
      const error = globalThis.chrome?.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve();
    });
  });
}

function storageRemove(storage, keys) {
  return new Promise((resolve, reject) => {
    storage.remove(keys, () => {
      const error = globalThis.chrome?.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve();
    });
  });
}

function nonempty(value) {
  return typeof value === 'string' && value.length > 0;
}

function validatedBinding(value) {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== 3
    || !nonempty(value.creator_account_id)
    || !nonempty(value.auth_ticket)
    || !nonempty(value.storage_bootstrap)
  ) throw new Error('A Brain-authorized encrypted session binding is required');
  return {
    creatorAccountId: value.creator_account_id,
    authTicket: value.auth_ticket,
    storageBootstrap: value.storage_bootstrap,
  };
}

function validatedReconnectCredential(value) {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== 4
    || !nonempty(value.creator_account_id)
    || !nonempty(value.auth_ticket)
    || !nonempty(value.config_auth_ticket)
    || !nonempty(value.agent_installation_id)
  ) throw new Error('The Agent reconnect credential rotation is invalid');
  return {
    creatorAccountId: value.creator_account_id,
    authTicket: value.auth_ticket,
    configAuthTicket: value.config_auth_ticket,
    agentInstallationId: value.agent_installation_id,
  };
}

function validatedUnlock(value) {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== 5
    || value.schema !== UNLOCK_SCHEMA
    || !nonempty(value.creator_account_id)
    || !['pairing', 'reconnect'].includes(value.credential_kind)
    || !nonempty(value.auth_ticket)
    || !nonempty(value.storage_key_base64)
  ) throw new Error('Brain returned an invalid Full-mode storage unlock');
  let key;
  try {
    key = atob(value.storage_key_base64);
  } catch (_error) {
    throw new Error('Brain returned an invalid Full-mode storage unlock');
  }
  if (key.length !== 32) throw new Error('Brain returned an invalid Full-mode storage unlock');
  return {
    creatorAccountId: value.creator_account_id,
    credentialKind: value.credential_kind,
    authTicket: value.auth_ticket,
    storageKey: value.storage_key_base64,
  };
}

function validatedRotation(value) {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== 2
    || value.schema !== ROTATION_SCHEMA
    || !nonempty(value.storage_bootstrap)
  ) throw new Error('Brain returned an invalid Full-mode storage rotation');
  return value.storage_bootstrap;
}

async function jsonPost(fetchImpl, endpoint, body, authorization = null, context = {}) {
  let response;
  try {
    response = await fetchImpl(endpoint, {
      method: 'POST',
      signal: context.signal,
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        ...(authorization === null ? {} : { Authorization: `Bearer ${authorization}` }),
      },
      body: JSON.stringify(body),
      cache: 'no-store',
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
    });
  } catch (error) {
    context.signal?.throwIfAborted();
    throw error;
  }
  if (!response?.ok) throw new Error('The local Brain refused Full-mode storage');
  try {
    return await response.json();
  } catch (_error) {
    throw new Error('Brain returned a non-JSON Full-mode storage response');
  }
}

function deleteDatabase(indexedDb, databaseName) {
  if (typeof indexedDb?.deleteDatabase !== 'function') {
    throw new Error('IndexedDB plaintext cleanup is unavailable');
  }
  return new Promise((resolve, reject) => {
    const request = indexedDb.deleteDatabase(databaseName);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(
      request.error ?? new Error(`Unable to delete legacy IndexedDB ${databaseName}`),
    );
    request.onblocked = () => reject(
      new Error(`Legacy IndexedDB deletion for ${databaseName} is blocked`),
    );
  });
}

export function createChromeAdapterCore({
  chromeApi = globalThis.chrome,
  idFactory = () => crypto.randomUUID(),
  indexedDb = globalThis.indexedDB,
  cryptoApi = globalThis.crypto,
  fetchImpl = globalThis.fetch,
  accountDatabaseName,
  encryptedPrefix,
  legacyPrefix,
  listDatabases = null,
  storageUnsealEndpoint = LOCAL_SERVICE_STORAGE_UNSEAL,
  storageRotateEndpoint = LOCAL_SERVICE_STORAGE_ROTATE,
}) {
  if (!chromeApi?.storage?.local) throw new Error('chrome.storage.local is unavailable');
  if (typeof fetchImpl !== 'function') throw new Error('fetch is unavailable');
  if (listDatabases !== null && typeof listDatabases !== 'function') {
    throw new Error('IndexedDB plaintext cleanup enumeration is invalid');
  }
  assertLocalServiceUrl(storageUnsealEndpoint);
  assertLocalServiceUrl(storageRotateEndpoint);
  fetchImpl = createSecureLocalFetch({ fetchImpl, maxResponseBytes: 16 * 1024 });
  const installationStorage = chromeApi.storage.local;
  const sessionStorage = chromeApi.storage.session;
  let cached = null;
  let cacheGeneration = 0;
  const pending = new Set();
  const controllers = new Set();
  let cleanup = Promise.resolve();
  function invalidate() {
    cacheGeneration += 1;
    cached = null;
    for (const controller of controllers) {
      controller.abort(Object.assign(new Error('stale_binding'), { code: 'stale_binding' }));
    }
  }
  function run(operation, args, context = {}) {
    const generation = cacheGeneration;
    const controller = new AbortController();
    controllers.add(controller);
    const abort = () => controller.abort(context.signal.reason);
    context.signal?.addEventListener('abort', abort, { once: true });
    if (context.signal?.aborted) abort();
    const assertCurrent = () => {
      controller.signal.throwIfAborted();
      if (generation !== cacheGeneration) throw Object.assign(new Error('stale_binding'), { code: 'stale_binding' });
      context.assertCurrent?.();
    };
    const attempt = cleanup.then(async () => {
      assertCurrent();
      const result = await operation(...args, { signal: controller.signal, assertCurrent });
      assertCurrent();
      return result;
    });
    pending.add(attempt);
    void attempt.finally(() => {
      pending.delete(attempt);
      controllers.delete(controller);
      context.signal?.removeEventListener('abort', abort);
    }).catch(() => undefined);
    return attempt;
  }

  async function savedBootstrap() {
    const value = await storageGet(installationStorage, [FULL_STORAGE_BOOTSTRAP_KEY]);
    const bootstrap = value[FULL_STORAGE_BOOTSTRAP_KEY];
    if (bootstrap === undefined) return null;
    if (!nonempty(bootstrap)) throw new Error('The stored Full-mode bootstrap is invalid');
    return bootstrap;
  }

  async function unlock(bootstrap, context) {
    context.assertCurrent();
    if (cached?.bootstrap === bootstrap) return cached.unlock;
    const document = await jsonPost(fetchImpl, storageUnsealEndpoint, {
      storage_bootstrap: bootstrap,
    }, null, context);
    context.assertCurrent();
    const unlocked = validatedUnlock(document);
    cached = { bootstrap, unlock: unlocked };
    return unlocked;
  }

  async function encryptedPartitionName(creatorAccountId) {
    return accountDatabaseName(creatorAccountId, cryptoApi);
  }

  async function deleteLegacyPartitions() {
    const enumerate = listDatabases ?? (async () => {
      if (typeof indexedDb?.databases !== 'function') {
        throw new Error('IndexedDB plaintext cleanup enumeration is unavailable');
      }
      return indexedDb.databases();
    });
    const databases = await enumerate();
    if (!Array.isArray(databases)) {
      throw new Error('IndexedDB plaintext cleanup enumeration is invalid');
    }
    const prefix = `${legacyPrefix}-`;
    const legacyNames = databases
      .map((entry) => entry?.name)
      .filter((name) => typeof name === 'string' && name.startsWith(prefix));
    for (const legacyName of legacyNames) {
      await deleteDatabase(indexedDb, legacyName);
    }
  }

  async function scrubGlobalCredentials() {
    await storageRemove(installationStorage, GLOBAL_CREDENTIAL_KEYS_TO_REMOVE);
    if (sessionStorage) await storageRemove(sessionStorage, GLOBAL_CREDENTIAL_KEYS_TO_REMOVE);
  }

  const adapter = {
    async loadAgentInstallationId() {
      const saved = await storageGet(installationStorage, [INSTALLATION_ID_KEY]);
      const agentInstallationId = saved[INSTALLATION_ID_KEY] ?? idFactory();
      if (!nonempty(agentInstallationId)) throw new Error('Agent installation identity is invalid');
      await storageSet(installationStorage, { [INSTALLATION_ID_KEY]: agentInstallationId });
      return agentInstallationId;
    },

    async loadAgentIdentity() {
      return { agentInstallationId: await this.loadAgentInstallationId() };
    },

    async loadBrainBinding(context) {
      const bootstrap = await savedBootstrap();
      if (bootstrap === null) throw new Error('A Brain-authorized encrypted session binding is required');
      const unlocked = await unlock(bootstrap, context);
      const databaseName = await encryptedPartitionName(unlocked.creatorAccountId);
      if (!sessionStorage) throw new Error('chrome.storage.session is unavailable');
      context.assertCurrent();
      await storageSet(sessionStorage, { [ACTIVE_ACCOUNT_PARTITION_KEY]: databaseName });
      return {
        creatorAccountId: unlocked.creatorAccountId,
        authTicket: unlocked.authTicket,
        credentialKind: unlocked.credentialKind,
        storageKey: unlocked.storageKey,
      };
    },

    async loadReconnectAuthTicket(creatorAccountId, context) {
      if (!nonempty(creatorAccountId)) {
        throw new Error('A creator account is required to load an Agent reconnect credential');
      }
      const bootstrap = await savedBootstrap();
      if (bootstrap === null) return null;
      const unlocked = await unlock(bootstrap, context);
      if (unlocked.creatorAccountId !== creatorAccountId || unlocked.credentialKind !== 'reconnect') {
        return null;
      }
      return unlocked.authTicket;
    },

    async saveReconnectAuthTicket({
      creatorAccountId,
      authTicket,
      configAuthTicket,
      agentInstallationId,
    }, context) {
      const credential = validatedReconnectCredential({
        creator_account_id: creatorAccountId,
        auth_ticket: authTicket,
        config_auth_ticket: configAuthTicket,
        agent_installation_id: agentInstallationId,
      });
      const bootstrap = await savedBootstrap();
      if (bootstrap === null) throw new Error('A matching encrypted Brain binding is required');
      const unlocked = await unlock(bootstrap, context);
      if (unlocked.creatorAccountId !== credential.creatorAccountId) {
        throw new Error('A matching encrypted Brain binding is required');
      }
      const document = await jsonPost(
        fetchImpl,
        storageRotateEndpoint,
        {
          protocol_version: '2',
          creator_account_id: credential.creatorAccountId,
          agent_installation_id: credential.agentInstallationId,
          reconnect_auth_ticket: credential.authTicket,
          storage_bootstrap: bootstrap,
        },
        credential.configAuthTicket,
        context,
      );
      context.assertCurrent();
      const rotated = validatedRotation(document);
      await storageSet(installationStorage, { [FULL_STORAGE_BOOTSTRAP_KEY]: rotated });
      context.assertCurrent();
      cached = {
        bootstrap: rotated,
        unlock: {
          ...unlocked,
          credentialKind: 'reconnect',
          authTicket: credential.authTicket,
        },
      };
    },

    async saveBrainBinding({ creatorAccountId, authTicket, storageBootstrap }, context) {
      const binding = validatedBinding({
        creator_account_id: creatorAccountId,
        auth_ticket: authTicket,
        storage_bootstrap: storageBootstrap,
      });
      if (!sessionStorage) throw new Error('chrome.storage.session is unavailable');
      const unlocked = await unlock(binding.storageBootstrap, context);
      if (
        unlocked.creatorAccountId !== binding.creatorAccountId
        || unlocked.authTicket !== binding.authTicket
        || unlocked.credentialKind !== 'pairing'
      ) throw new Error('The sealed Brain binding does not match its pairing credential');
      let durableBootstrap = binding.storageBootstrap;
      let durableUnlock = unlocked;
      const existingBootstrap = await savedBootstrap();
      if (existingBootstrap !== null && existingBootstrap !== binding.storageBootstrap) {
        const existing = await unlock(existingBootstrap, context);
        if (
          existing.creatorAccountId === binding.creatorAccountId
          && existing.credentialKind === 'reconnect'
        ) {
          durableBootstrap = existingBootstrap;
          durableUnlock = existing;
        }
      }
      try {
        // A pre-release browser may contain plaintext partitions for accounts
        // other than the account being rebound now. Full mode must not resume
        // until every legacy plaintext account partition has been removed.
        await deleteLegacyPartitions();
      } catch (error) {
        cached = null;
        throw error;
      }
      const databaseName = await encryptedPartitionName(binding.creatorAccountId);
      context.assertCurrent();
      await storageSet(installationStorage, {
        [FULL_STORAGE_BOOTSTRAP_KEY]: durableBootstrap,
      });
      context.assertCurrent();
      await storageSet(sessionStorage, {
        [ACTIVE_ACCOUNT_PARTITION_KEY]: databaseName,
      });
      context.assertCurrent();
      await scrubGlobalCredentials();
      context.assertCurrent();
      cached = { bootstrap: durableBootstrap, unlock: durableUnlock };
      return {
        creatorAccountId: binding.creatorAccountId,
        authTicket: durableUnlock.authTicket,
        credentialKind: durableUnlock.credentialKind,
        storageKey: durableUnlock.storageKey,
      };
    },

    async clearBrainBinding() {
      cached = null;
      await storageRemove(installationStorage, [
        FULL_STORAGE_BOOTSTRAP_KEY,
        ...GLOBAL_CREDENTIAL_KEYS_TO_REMOVE,
      ]);
      if (sessionStorage) {
        await storageRemove(sessionStorage, [
          ACTIVE_ACCOUNT_PARTITION_KEY,
          ...GLOBAL_CREDENTIAL_KEYS_TO_REMOVE,
        ]);
      }
    },

    onWake(listener) {
      const events = [
        chromeApi.runtime?.onStartup,
        chromeApi.runtime?.onInstalled,
        chromeApi.runtime?.onMessage,
        chromeApi.tabs?.onUpdated,
      ].filter((event) => event?.addListener);
      const wrappers = events.map((event) => {
        const wrapper = () => listener();
        event.addListener(wrapper);
        return [event, wrapper];
      });
      const alarmEvent = chromeApi.alarms?.onAlarm;
      const alarmWrapper = (alarm) => {
        if (alarm?.name === RECONCILE_ALARM_NAME) listener();
      };
      if (alarmEvent?.addListener) alarmEvent.addListener(alarmWrapper);
      const alarmCreation = chromeApi.alarms?.create?.(RECONCILE_ALARM_NAME, {
        delayInMinutes: 1,
        periodInMinutes: 1,
      });
      alarmCreation?.catch?.(() => undefined);
      return () => {
        wrappers.forEach(([event, wrapper]) => event.removeListener?.(wrapper));
        alarmEvent?.removeListener?.(alarmWrapper);
      };
    },
  };
  return Object.freeze({
    ...adapter,
    loadBrainBinding: (context = {}) => run(adapter.loadBrainBinding, [], context),
    loadReconnectAuthTicket: (account, context = {}) => run(adapter.loadReconnectAuthTicket, [account], context),
    saveReconnectAuthTicket: (credential, context = {}) => run(adapter.saveReconnectAuthTicket, [credential], context),
    saveBrainBinding(binding, context = {}) {
      invalidate();
      cleanup = Promise.allSettled([...pending, cleanup]).then(() => undefined);
      return run(adapter.saveBrainBinding, [binding], context);
    },
    clearBrainBinding() {
      invalidate();
      cleanup = Promise.allSettled([...pending, cleanup]).then(() => adapter.clearBrainBinding());
      return cleanup;
    },
  });
}

export function createBrainBindingBridgeCore({
  chromeApi = globalThis.chrome,
  adapter,
  runtime,
  onBound = null,
  ensureReady = async () => {},
  allowsBinding = () => true,
  runBindingOperation = (operation) => operation({}),
  allowedOrigins = [LOCAL_SERVICE_ORIGIN],
} = {}) {
  if (!chromeApi?.runtime?.onMessageExternal?.addListener) {
    throw new Error('chrome.runtime.onMessageExternal is unavailable');
  }
  if (typeof adapter?.saveBrainBinding !== 'function') {
    throw new Error('Brain binding bridge requires a Chrome adapter');
  }
  if (typeof runtime?.wake !== 'function') {
    throw new Error('Brain binding bridge requires an Agent runtime');
  }
  if (onBound !== null && typeof onBound !== 'function') {
    throw new Error('Brain binding bridge onBound must be a function');
  }
  const origins = new Set(allowedOrigins);
  if ([...origins].some((origin) => origin !== LOCAL_SERVICE_ORIGIN)) {
    throw new Error('invalid_local_service_endpoint');
  }
  let registered = false;
  const listener = (message, sender, sendResponse) => {
    let origin = null;
    try {
      origin = typeof sender?.url === 'string' ? new URL(sender.url).origin : null;
    } catch {
      origin = null;
    }
    if (origin === null || !origins.has(origin)) return false;
    if (
      typeof message !== 'object'
      || message === null
      || Array.isArray(message)
      || Object.keys(message).length !== 5
      || message.type !== BRAIN_BINDING_MESSAGE_TYPE
      || message.protocol_version !== '2'
      || !nonempty(message.creator_account_id)
      || !nonempty(message.auth_ticket)
      || !nonempty(message.storage_bootstrap)
    ) {
      sendResponse({ ok: false, code: 'invalid_binding' });
      return false;
    }
    void Promise.resolve().then(async () => {
      await ensureReady();
      return runBindingOperation(async (context = {}) => {
        context.assertCurrent?.();
        if (!allowsBinding()) throw new Error('binding_disabled');
        const result = await adapter.saveBrainBinding({
          creatorAccountId: message.creator_account_id,
          authTicket: message.auth_ticket,
          storageBootstrap: message.storage_bootstrap,
        }, context);
        context.assertCurrent?.();
        if (!allowsBinding()) throw new Error('binding_disabled');
        return result;
      });
    }).then(
      async () => {
        try {
          if (onBound === null) await runtime.wake();
          else await onBound();
          sendResponse({ ok: true });
        } catch {
          sendResponse({ ok: false, code: 'agent_start_failed' });
        }
      },
      () => sendResponse({ ok: false, code: 'binding_persist_failed' }),
    );
    return true;
  };
  return Object.freeze({
    register() {
      if (registered) return;
      chromeApi.runtime.onMessageExternal.addListener(listener);
      registered = true;
    },
    unregister() {
      if (!registered) return;
      chromeApi.runtime.onMessageExternal.removeListener?.(listener);
      registered = false;
    },
  });
}
