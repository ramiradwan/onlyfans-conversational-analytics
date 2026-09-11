import {
  captureSenderKey,
  isCaptureEpoch,
} from '../runtime/capture-context.mjs';

import { LOCAL_SERVICE_ORIGIN } from './local-service-endpoints.mjs';

export const PROVISIONING_IDENTITY_MESSAGE_TYPE = 'ofca.provisioning.identity.update';
export const PROVISIONING_IDENTITY_VERSION = 1;
export const PROVISIONING_IDENTITY_STORAGE_KEY = 'provisioning_authenticated_profile_v1';
export const PROVISIONING_IDENTITY_STORAGE_SCHEMA = 'ofca-provisioning-identities/v2';
export const PROVISIONING_IDENTITY_QUERY_TYPE = 'provisioning.identity.query';
export const PROVISIONING_IDENTITY_RESULT_TYPE = 'provisioning.identity.result';

const MAX_CONTEXTS = 32;

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasExactKeys(value, expected) {
  const keys = Object.keys(value);
  return keys.length === expected.length && keys.every((key) => expected.includes(key));
}

function profileAccountId(profile) {
  if (
    !isRecord(profile)
    || !hasExactKeys(profile, ['creator_account_id'])
    || typeof profile.creator_account_id !== 'string'
    || profile.creator_account_id.length < 1
    || profile.creator_account_id.length > 200
  ) return undefined;
  return profile.creator_account_id;
}

function identityUpdate(message) {
  if (
    !isRecord(message)
    || !hasExactKeys(message, ['type', 'version', 'page_epoch', 'authenticated_profile'])
    || message.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE
    || message.version !== PROVISIONING_IDENTITY_VERSION
    || !isCaptureEpoch(message.page_epoch)
  ) return undefined;
  if (message.authenticated_profile === null) {
    return { accountId: null, pageEpoch: message.page_epoch };
  }
  const accountId = profileAccountId(message.authenticated_profile);
  return accountId === undefined
    ? undefined
    : { accountId, pageEpoch: message.page_epoch };
}

function isIdentityQuery(message) {
  return isRecord(message)
    && hasExactKeys(message, ['type', 'version'])
    && message.type === PROVISIONING_IDENTITY_QUERY_TYPE
    && message.version === PROVISIONING_IDENTITY_VERSION;
}

function senderOrigin(sender) {
  try {
    return typeof sender?.url === 'string' ? new URL(sender.url).origin : null;
  } catch (_error) {
    return null;
  }
}

function storageGet(storage, key, chromeApi) {
  return new Promise((resolve, reject) => {
    storage.get([key], (stored) => {
      const error = chromeApi.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve(stored ?? {});
    });
  });
}

function storageSet(storage, value, chromeApi) {
  return new Promise((resolve, reject) => {
    storage.set(value, () => {
      const error = chromeApi.runtime?.lastError;
      if (error) reject(new Error(error.message));
      else resolve();
    });
  });
}

function validStoredContext(value) {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value);
  const baseKeys = [
    'sender_key',
    'tab_id',
    'document_id',
    'page_epoch',
    'observed_platform_id',
    'consent_epoch',
  ];
  const validKeys = keys.length === baseKeys.length
    ? hasExactKeys(value, baseKeys)
    : hasExactKeys(value, [...baseKeys, 'identity_conflict']);
  return validKeys
    && (value.identity_conflict === undefined || value.identity_conflict === true)
    && typeof value.sender_key === 'string'
    && value.sender_key.length > 0
    && Number.isInteger(value.tab_id)
    && typeof value.document_id === 'string'
    && value.document_id.length > 0
    && value.sender_key === `${value.tab_id}:${value.document_id}`
    && isCaptureEpoch(value.consent_epoch)
    && isCaptureEpoch(value.page_epoch)
    && (
      value.observed_platform_id === null
      || (
        typeof value.observed_platform_id === 'string'
        && value.observed_platform_id.length >= 1
        && value.observed_platform_id.length <= 200
      )
    );
}

function normalizeContexts(value) {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ['schema', 'contexts'])
    || value.schema !== PROVISIONING_IDENTITY_STORAGE_SCHEMA
    || !Array.isArray(value.contexts)
  ) return [];
  return value.contexts
    .filter(validStoredContext)
    .slice(-MAX_CONTEXTS)
    .map((context) => structuredClone(context));
}

function storedDocument(contexts) {
  return {
    schema: PROVISIONING_IDENTITY_STORAGE_SCHEMA,
    contexts: contexts.slice(-MAX_CONTEXTS).map((context) => structuredClone(context)),
  };
}

function result(accountId) {
  return {
    type: PROVISIONING_IDENTITY_RESULT_TYPE,
    version: PROVISIONING_IDENTITY_VERSION,
    authenticated_profile: accountId === null ? null : { creator_account_id: accountId },
  };
}

function provisioningAccountId(contexts) {
  if (contexts.some((context) => context.identity_conflict === true
    || context.observed_platform_id === null)) return null;
  const identities = new Set(
    contexts
      .map((context) => context.observed_platform_id)
      .filter((accountId) => typeof accountId === 'string'),
  );
  return identities.size === 1 ? [...identities][0] : null;
}

export function createProvisioningIdentityBridge({
  chromeApi = globalThis.chrome,
  allowedOrigins = [LOCAL_SERVICE_ORIGIN],
  currentConsent = () => null,
  allowsIdentity = () => currentConsent()?.mode === 'full',
  ensureReady = async () => {},
} = {}) {
  if (!chromeApi?.runtime?.onMessage?.addListener) {
    throw new Error('chrome.runtime.onMessage is unavailable');
  }
  if (!chromeApi?.runtime?.onMessageExternal?.addListener) {
    throw new Error('chrome.runtime.onMessageExternal is unavailable');
  }
  if (!chromeApi?.storage?.session) throw new Error('chrome.storage.session is unavailable');

  const origins = new Set(allowedOrigins);
  const sessionStorage = chromeApi.storage.session;
  let registered = false;
  let contextQueue = Promise.resolve();
  let generation = 0;
  const documentGenerations = new Map();
  const invalidateDocument = (tabId) => {
    documentGenerations.set(tabId, (documentGenerations.get(tabId) ?? 0) + 1);
    if (documentGenerations.size > MAX_CONTEXTS) {
      generation += 1;
      documentGenerations.delete(documentGenerations.keys().next().value);
    }
  };

  const serializeContext = (work) => {
    const operation = contextQueue.then(work);
    contextQueue = operation.catch(() => undefined);
    return operation;
  };

  const loadContexts = async () => {
    const stored = await storageGet(sessionStorage, PROVISIONING_IDENTITY_STORAGE_KEY, chromeApi);
    return normalizeContexts(stored[PROVISIONING_IDENTITY_STORAGE_KEY])
      .filter((context) => context.consent_epoch === currentConsent()?.consent_epoch);
  };

  const internalListener = (message, sender, sendResponse) => {
    if (message?.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE) return false;
    const update = identityUpdate(message);
    if (update === undefined) return false;
    let senderKey;
    try {
      senderKey = captureSenderKey(sender, chromeApi.runtime.id);
    } catch (_error) {
      return false;
    }
    // Fence admitted captures before waiting for persistence or readiness.
    invalidateDocument(sender.tab.id);
    const admittedGeneration = generation;
    const admittedConsentEpoch = currentConsent()?.consent_epoch;
    void (async () => {
      await ensureReady();
      if (!allowsIdentity() || (admittedConsentEpoch !== undefined
        && admittedConsentEpoch !== currentConsent()?.consent_epoch)) return { ok: false };
      return serializeContext(async () => {
        if (generation !== admittedGeneration || !allowsIdentity()) return { ok: false };
        const contexts = await loadContexts();
        const retained = contexts.filter((context) => context.tab_id !== sender.tab.id);
        retained.push({
          sender_key: senderKey, tab_id: sender.tab.id, document_id: sender.documentId,
          page_epoch: update.pageEpoch, observed_platform_id: update.accountId,
          consent_epoch: currentConsent().consent_epoch,
        });
        await storageSet(sessionStorage, {
          [PROVISIONING_IDENTITY_STORAGE_KEY]: storedDocument(retained),
        }, chromeApi);
        return { ok: true };
      });
    })().then(sendResponse, () => sendResponse({ ok: false }));
    return true;
  };

  const externalListener = (message, sender, sendResponse) => {
    if (!origins.has(senderOrigin(sender)) || !isIdentityQuery(message)) return false;
    void (async () => {
      await ensureReady();
      if (!allowsIdentity()) return result(null);
      return serializeContext(async () => result(provisioningAccountId(await loadContexts())));
    })().then(sendResponse, () => sendResponse(result(null)));
    return true;
  };

  const contextForSender = async (sender) => {
    const senderKey = captureSenderKey(sender, chromeApi.runtime.id);
    const contexts = await loadContexts();
    const context = contexts.find((candidate) => candidate.sender_key === senderKey);
    return context === undefined ? null : structuredClone(context);
  };

  const clearContexts = () => {
    generation += 1;
    documentGenerations.clear();
    return serializeContext(() => storageSet(sessionStorage, {
      [PROVISIONING_IDENTITY_STORAGE_KEY]: storedDocument([]),
    }, chromeApi));
  };
  const removeTab = (tabId) => {
    invalidateDocument(tabId);
    void serializeContext(async () => {
      const retained = (await loadContexts()).filter((context) => context.tab_id !== tabId);
      await storageSet(sessionStorage, {
        [PROVISIONING_IDENTITY_STORAGE_KEY]: storedDocument(retained),
      }, chromeApi);
    }).catch(() => undefined);
  };
  const tabUpdated = (tabId, changeInfo) => {
    if (changeInfo.status === 'loading' || changeInfo.url !== undefined) removeTab(tabId);
  };
  const resetSession = () => { void clearContexts().catch(() => undefined); };

  return Object.freeze({
    clearContexts,
    contextFor(sender) {
      return serializeContext(() => contextForSender(sender));
    },
    withCaptureContext(sender, work) {
      if (typeof work !== 'function') throw new TypeError('Capture context work is required');
      return serializeContext(async () => {
        const snapshot = { generation, documentGeneration: documentGenerations.get(sender.tab.id) ?? 0 };
        return { ...snapshot, context: await contextForSender(sender) };
      }).then((snapshot) => work(snapshot.context, () => {
        if (snapshot.generation !== generation
          || snapshot.documentGeneration !== (documentGenerations.get(sender.tab.id) ?? 0)) {
          throw Object.assign(new Error('stale_capture_context'), { code: 'stale_capture_context', retryable: false });
        }
      }));
    },
    register() {
      if (registered) return;
      chromeApi.runtime.onMessage.addListener(internalListener);
      chromeApi.runtime.onMessageExternal.addListener(externalListener);
      chromeApi.tabs?.onRemoved?.addListener(removeTab);
      chromeApi.tabs?.onUpdated?.addListener(tabUpdated);
      chromeApi.runtime.onStartup?.addListener(resetSession);
      chromeApi.runtime.onInstalled?.addListener(resetSession);
      registered = true;
    },
    unregister() {
      if (!registered) return;
      chromeApi.runtime.onMessage.removeListener?.(internalListener);
      chromeApi.runtime.onMessageExternal.removeListener?.(externalListener);
      chromeApi.tabs?.onRemoved?.removeListener?.(removeTab);
      chromeApi.tabs?.onUpdated?.removeListener?.(tabUpdated);
      chromeApi.runtime.onStartup?.removeListener?.(resetSession);
      chromeApi.runtime.onInstalled?.removeListener?.(resetSession);
      registered = false;
    },
  });
}
