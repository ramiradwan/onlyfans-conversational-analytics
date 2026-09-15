import { captureSenderKey, isCaptureEpoch } from './capture-context.mjs';
import {
  PROVISIONING_IDENTITY_MESSAGE_TYPE,
  PROVISIONING_IDENTITY_VERSION,
} from '../transport/provisioning-identity.mjs';

function exactKeys(value, keys) {
  return value !== null
    && typeof value === 'object'
    && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function observedAccountId(message) {
  if (
    !exactKeys(message, ['type', 'version', 'page_epoch', 'authenticated_profile'])
    || message.type !== PROVISIONING_IDENTITY_MESSAGE_TYPE
    || message.version !== PROVISIONING_IDENTITY_VERSION
    || !isCaptureEpoch(message.page_epoch)
  ) return undefined;
  if (message.authenticated_profile === null) return null;
  if (!exactKeys(message.authenticated_profile, ['creator_account_id'])) return undefined;
  const accountId = message.authenticated_profile.creator_account_id;
  return typeof accountId === 'string' && accountId.length >= 1 && accountId.length <= 200
    ? accountId
    : undefined;
}

/**
 * Keep the authenticated companion account-scoped without coupling its lifetime
 * to a specific OnlyFans document. Capture admission has its own document/epoch
 * fence; this guard only tears down the companion when a trusted platform
 * identity observation proves sign-out or an actual creator switch.
 */
export function createProvisioningCompanionGuard({
  chromeApi = globalThis.chrome,
  companionClient,
  configuredPlatformIdentity = () => null,
} = {}) {
  if (!chromeApi?.runtime?.onMessage?.addListener) {
    throw new Error('chrome.runtime.onMessage is unavailable');
  }
  if (!companionClient || typeof companionClient.invalidate !== 'function') {
    throw new TypeError('A companion client is required');
  }
  if (typeof configuredPlatformIdentity !== 'function') {
    throw new TypeError('A configured platform identity resolver is required');
  }

  let registered = false;
  let lastStablePlatformIdentity = null;
  const listener = (message, sender) => {
    const observed = observedAccountId(message);
    if (observed === undefined) return false;
    try {
      captureSenderKey(sender, chromeApi.runtime.id);
    } catch (_error) {
      return false;
    }

    let configured = null;
    try {
      const value = configuredPlatformIdentity();
      if (typeof value === 'string' && value.length > 0) configured = value;
    } catch (_error) {
      companionClient.invalidate();
      return false;
    }
    const expected = configured ?? lastStablePlatformIdentity;
    const conflict = observed === null || (expected !== null && observed !== expected);
    if (conflict) companionClient.invalidate();
    lastStablePlatformIdentity = observed;
    return false;
  };

  return Object.freeze({
    register() {
      if (registered) return;
      chromeApi.runtime.onMessage.addListener(listener);
      registered = true;
    },
    unregister() {
      if (!registered) return;
      chromeApi.runtime.onMessage.removeListener?.(listener);
      registered = false;
    },
  });
}
