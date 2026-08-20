export const PROVISIONING_IDENTITY_MESSAGE_TYPE = 'ofca.provisioning.identity.update';
export const PROVISIONING_IDENTITY_VERSION = 1;
export const PROVISIONING_IDENTITY_STORAGE_KEY = 'provisioning_authenticated_profile_v1';
export const PROVISIONING_IDENTITY_QUERY_TYPE = 'provisioning.identity.query';
export const PROVISIONING_IDENTITY_RESULT_TYPE = 'provisioning.identity.result';

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

function identityUpdateAccountId(message) {
  if (
    !isRecord(message)
    || !hasExactKeys(message, ['type', 'version', 'authenticated_profile'])
    || message.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE
    || message.version !== PROVISIONING_IDENTITY_VERSION
  ) return undefined;
  if (message.authenticated_profile === null) return null;
  return profileAccountId(message.authenticated_profile);
}

function isIdentityQuery(message) {
  return isRecord(message)
    && hasExactKeys(message, ['type', 'version'])
    && message.type === PROVISIONING_IDENTITY_QUERY_TYPE
    && message.version === PROVISIONING_IDENTITY_VERSION;
}

function isTrustedContentSender(sender, chromeApi) {
  if (sender?.id !== chromeApi.runtime.id || sender?.frameId !== 0) return false;
  try {
    return new URL(sender.url).origin === 'https://onlyfans.com';
  } catch (_error) {
    return false;
  }
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

function result(accountId) {
  return {
    type: PROVISIONING_IDENTITY_RESULT_TYPE,
    version: PROVISIONING_IDENTITY_VERSION,
    authenticated_profile: accountId === null ? null : { creator_account_id: accountId },
  };
}

/**
 * Relays the signed-in platform identity to session-only storage and answers provisioning reads.
 * The manifest is the first external-origin gate; this exact check prevents a broadened match
 * from silently granting another localhost port access to the provisioning identity.
 */
export function createProvisioningIdentityBridge({
  chromeApi = globalThis.chrome,
  allowedOrigins = ['http://bridge.localhost:17871'],
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
  const internalListener = (message, sender, sendResponse) => {
    if (!isTrustedContentSender(sender, chromeApi)) return false;
    if (message?.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE) return false;
    const accountId = identityUpdateAccountId(message);
    if (accountId === undefined) return false;
    void storageSet(sessionStorage, {
      [PROVISIONING_IDENTITY_STORAGE_KEY]: accountId === null
        ? null
        : { creator_account_id: accountId },
    }, chromeApi).then(
      () => sendResponse({ ok: true }),
      () => sendResponse({ ok: false }),
    );
    return true;
  };
  const externalListener = (message, sender, sendResponse) => {
    if (!origins.has(senderOrigin(sender)) || !isIdentityQuery(message)) return false;
    void storageGet(sessionStorage, PROVISIONING_IDENTITY_STORAGE_KEY, chromeApi).then(
      (stored) => {
        const accountId = profileAccountId(stored[PROVISIONING_IDENTITY_STORAGE_KEY]);
        sendResponse(result(accountId === undefined ? null : accountId));
      },
      () => sendResponse(result(null)),
    );
    return true;
  };

  return Object.freeze({
    register() {
      if (registered) return;
      chromeApi.runtime.onMessage.addListener(internalListener);
      chromeApi.runtime.onMessageExternal.addListener(externalListener);
      registered = true;
    },
    unregister() {
      if (!registered) return;
      chromeApi.runtime.onMessage.removeListener?.(internalListener);
      chromeApi.runtime.onMessageExternal.removeListener?.(externalListener);
      registered = false;
    },
  });
}
