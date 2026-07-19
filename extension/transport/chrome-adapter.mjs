import { INGESTION_STORES } from './durable-outbox.mjs';
import {
  INGESTION_DATABASE_NAME_PREFIX,
  accountDatabaseName,
  createIndexedDbIngestionStorage,
} from './indexeddb-ingestion-storage.mjs';

/** Centralizes the deliberately small installation-global chrome.* surface. */

const INSTALLATION_ID_KEY = 'agent_installation_id';
export const ACTIVE_ACCOUNT_PARTITION_KEY = 'active_account_partition_v4';
export const BRAIN_BINDING_MESSAGE_TYPE = 'ofca.agent.bind';
export const RECONCILE_ALARM_NAME = 'ofca-agent-reconcile';
const ACCOUNT_CREDENTIAL_KEY = 'brain';
const LEGACY_GLOBAL_CREDENTIAL_KEYS = Object.freeze([
  'brain_binding_v2',
  'agent_reconnect_auth_v2',
]);
const PARTITION_PATTERN = new RegExp(
  `^${INGESTION_DATABASE_NAME_PREFIX}-[a-f0-9]{64}$`,
);

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

function validatedBinding(value) {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== 2
    || typeof value.creator_account_id !== 'string'
    || value.creator_account_id.length === 0
    || typeof value.auth_ticket !== 'string'
    || value.auth_ticket.length === 0
  ) {
    throw new Error('A Brain-authorized session binding is required');
  }
  return {
    creatorAccountId: value.creator_account_id,
    authTicket: value.auth_ticket,
  };
}

function validatedReconnectCredential(value) {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== 2
    || typeof value.creator_account_id !== 'string'
    || value.creator_account_id.length === 0
    || typeof value.auth_ticket !== 'string'
    || value.auth_ticket.length === 0
  ) throw new Error('The stored Agent reconnect credential is invalid');
  return {
    creatorAccountId: value.creator_account_id,
    authTicket: value.auth_ticket,
  };
}

function validatedPartitionPointer(value) {
  if (typeof value !== 'string' || !PARTITION_PATTERN.test(value)) {
    throw new Error('The active account partition pointer is invalid');
  }
  return value;
}

function validatedCredentialRecord(value) {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== 4
    || value.key !== ACCOUNT_CREDENTIAL_KEY
    || typeof value.creator_account_id !== 'string'
    || value.creator_account_id.length === 0
    || typeof value.pairing_auth_ticket !== 'string'
    || value.pairing_auth_ticket.length === 0
    || (
      value.reconnect_auth_ticket !== null
      && (
        typeof value.reconnect_auth_ticket !== 'string'
        || value.reconnect_auth_ticket.length === 0
      )
    )
  ) throw new Error('The stored account credential is invalid');
  return structuredClone(value);
}

export function createChromeAdapter(
  chromeApi = globalThis.chrome,
  idFactory = () => crypto.randomUUID(),
  {
    indexedDb = globalThis.indexedDB,
    cryptoApi = globalThis.crypto,
  } = {},
) {
  if (!chromeApi?.storage?.local) throw new Error('chrome.storage.local is unavailable');
  const installationStorage = chromeApi.storage.local;
  const sessionStorage = chromeApi.storage.session;

  const partitionStorage = (databaseName) => createIndexedDbIngestionStorage(indexedDb, {
    databaseName,
    cryptoApi,
  });

  async function activePartition() {
    if (!sessionStorage) throw new Error('chrome.storage.session is unavailable');
    const saved = await storageGet(sessionStorage, [ACTIVE_ACCOUNT_PARTITION_KEY]);
    const pointer = saved[ACTIVE_ACCOUNT_PARTITION_KEY];
    return pointer === undefined ? null : validatedPartitionPointer(pointer);
  }

  async function loadCredential(databaseName) {
    const storage = partitionStorage(databaseName);
    const value = await storage.runTransaction(
      'readonly',
      [INGESTION_STORES.credentials],
      (tx) => tx.get(INGESTION_STORES.credentials, ACCOUNT_CREDENTIAL_KEY),
    );
    if (value === undefined) return null;
    const credential = validatedCredentialRecord(value);
    const expectedName = await accountDatabaseName(credential.creator_account_id, cryptoApi);
    if (expectedName !== databaseName) {
      throw new Error('The stored account credential does not match its partition');
    }
    return credential;
  }

  async function saveCredential(databaseName, credential) {
    const normalized = validatedCredentialRecord(credential);
    const expectedName = await accountDatabaseName(normalized.creator_account_id, cryptoApi);
    if (expectedName !== databaseName) {
      throw new Error('Refusing to write an account credential to the wrong partition');
    }
    await partitionStorage(databaseName).runTransaction(
      'readwrite',
      [INGESTION_STORES.credentials],
      (tx) => tx.put(INGESTION_STORES.credentials, normalized),
    );
  }

  async function deleteCredential(databaseName) {
    await partitionStorage(databaseName).runTransaction(
      'readwrite',
      [INGESTION_STORES.credentials],
      (tx) => tx.delete(INGESTION_STORES.credentials, ACCOUNT_CREDENTIAL_KEY),
    );
  }

  async function scrubLegacyGlobalCredentials() {
    if (sessionStorage) await storageRemove(sessionStorage, LEGACY_GLOBAL_CREDENTIAL_KEYS);
  }

  return Object.freeze({
    async loadAgentInstallationId() {
      const saved = await storageGet(installationStorage, [INSTALLATION_ID_KEY]);
      const agentInstallationId = saved[INSTALLATION_ID_KEY] ?? idFactory();
      await storageSet(installationStorage, { [INSTALLATION_ID_KEY]: agentInstallationId });
      return agentInstallationId;
    },

    async loadAgentIdentity() {
      return { agentInstallationId: await this.loadAgentInstallationId() };
    },

    async loadBrainBinding() {
      const databaseName = await activePartition();
      if (databaseName === null) throw new Error('A Brain-authorized session binding is required');
      const credential = await loadCredential(databaseName);
      return validatedBinding(credential === null ? null : {
        creator_account_id: credential.creator_account_id,
        auth_ticket: credential.pairing_auth_ticket,
      });
    },

    async loadReconnectAuthTicket(creatorAccountId) {
      if (typeof creatorAccountId !== 'string' || creatorAccountId.length === 0) {
        throw new Error('A creator account is required to load an Agent reconnect credential');
      }
      const databaseName = await activePartition();
      if (databaseName === null) return null;
      const expectedName = await accountDatabaseName(creatorAccountId, cryptoApi);
      if (databaseName !== expectedName) return null;
      const credential = await loadCredential(databaseName);
      return credential?.reconnect_auth_ticket ?? null;
    },

    async saveReconnectAuthTicket({ creatorAccountId, authTicket }) {
      const credential = validatedReconnectCredential({
        creator_account_id: creatorAccountId,
        auth_ticket: authTicket,
      });
      const databaseName = await activePartition();
      const expectedName = await accountDatabaseName(credential.creatorAccountId, cryptoApi);
      if (databaseName === null || databaseName !== expectedName) {
        throw new Error('A matching Brain binding is required to save a reconnect credential');
      }
      const saved = await loadCredential(databaseName);
      if (saved === null || saved.creator_account_id !== credential.creatorAccountId) {
        throw new Error('A matching account credential is required');
      }
      await saveCredential(databaseName, {
        ...saved,
        reconnect_auth_ticket: credential.authTicket,
      });
    },

    async saveBrainBinding({ creatorAccountId, authTicket }) {
      const binding = validatedBinding({
        creator_account_id: creatorAccountId,
        auth_ticket: authTicket,
      });
      if (!sessionStorage) throw new Error('chrome.storage.session is unavailable');
      const databaseName = await accountDatabaseName(binding.creatorAccountId, cryptoApi);
      const previousName = await activePartition();
      let reconnectAuthTicket = null;
      if (previousName === databaseName) {
        const saved = await loadCredential(databaseName);
        if (saved !== null) reconnectAuthTicket = saved.reconnect_auth_ticket;
      } else if (previousName !== null) {
        await deleteCredential(previousName);
      }
      await saveCredential(databaseName, {
        key: ACCOUNT_CREDENTIAL_KEY,
        creator_account_id: binding.creatorAccountId,
        pairing_auth_ticket: binding.authTicket,
        reconnect_auth_ticket: reconnectAuthTicket,
      });
      await storageSet(sessionStorage, {
        [ACTIVE_ACCOUNT_PARTITION_KEY]: databaseName,
      });
      await scrubLegacyGlobalCredentials();
      return binding;
    },

    async clearBrainBinding() {
      if (!sessionStorage) throw new Error('chrome.storage.session is unavailable');
      const databaseName = await activePartition();
      if (databaseName !== null) await deleteCredential(databaseName);
      await storageRemove(sessionStorage, [
        ACTIVE_ACCOUNT_PARTITION_KEY,
        ...LEGACY_GLOBAL_CREDENTIAL_KEYS,
      ]);
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
  });
}

/**
 * Receives one ephemeral, account-bound Brain ticket from an explicitly allowed Bridge origin.
 * The manifest is the first origin gate; this exact check prevents a broadened manifest from
 * silently becoming binding authority.
 */
export function createBrainBindingBridge({
  chromeApi = globalThis.chrome,
  adapter,
  runtime,
  allowedOrigins = ['http://bridge.localhost:17871'],
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
  const origins = new Set(allowedOrigins);
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
      || Object.keys(message).length !== 4
      || message.type !== BRAIN_BINDING_MESSAGE_TYPE
      || message.protocol_version !== '2'
      || typeof message.creator_account_id !== 'string'
      || message.creator_account_id.length === 0
      || typeof message.auth_ticket !== 'string'
      || message.auth_ticket.length === 0
    ) {
      sendResponse({ ok: false, code: 'invalid_binding' });
      return false;
    }
    void adapter.saveBrainBinding({
      creatorAccountId: message.creator_account_id,
      authTicket: message.auth_ticket,
    }).then(
      async () => {
        try {
          await runtime.wake();
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
