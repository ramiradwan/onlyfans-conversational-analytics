import {
  captureSenderKey,
  isCaptureEpoch,
} from '../runtime/capture-context.mjs';

import { PAGE_CONTROL_MESSAGE_TYPE, PAGE_CONTROL_VERSION,
  PROVISIONING_IDENTITY_RESET_TYPE, isProvisioningIdentityEnvelope } from '../capture/envelopes.mjs';
import { profileLabels, validProfileLabels } from '../capture/profile-labels.mjs';

export const PROVISIONING_IDENTITY_MESSAGE_TYPE = 'ofca.provisioning.identity.update';
export const PROVISIONING_IDENTITY_VERSION = 1;
export const PROVISIONING_IDENTITY_STORAGE_KEY = 'provisioning_authenticated_profile_v1';
export const PROVISIONING_IDENTITY_STORAGE_SCHEMA = 'ofca-provisioning-identities/v2';

const MAX_CONTEXTS = 32;
const PRESENTATION_PROOF_TIMEOUT_MS = 1000;

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
    || !hasExactKeys(profile, ['creator_account_id', ...['display_name', 'username'].filter(field => Object.hasOwn(profile, field))])
    || !validProfileLabels(profile)
    || typeof profile.creator_account_id !== 'string'
    || profile.creator_account_id.length < 1
    || profile.creator_account_id.length > 200
  ) return undefined;
  return profile.creator_account_id;
}

function identityUpdate(message) {
  if (message?.type === PROVISIONING_IDENTITY_RESET_TYPE && isProvisioningIdentityEnvelope(message)) {
    return { accountId: null, pageEpoch: message.page_epoch };
  }
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
    : { accountId, pageEpoch: message.page_epoch, ...profileLabels(message.authenticated_profile) };
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
    ...['incognito'].filter(field => Object.hasOwn(value, field)),
    ...['display_name', 'username'].filter(field => Object.hasOwn(value, field)),
  ];
  const validKeys = keys.length === baseKeys.length
    ? hasExactKeys(value, baseKeys)
    : hasExactKeys(value, [...baseKeys, 'identity_conflict']);
  return validKeys && validProfileLabels(value)
    && (value.incognito === undefined || typeof value.incognito === 'boolean')
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
  currentConsent = () => null,
  allowsIdentity = () => currentConsent()?.mode === 'full',
  ensureReady = async () => {},
} = {}) {
  if (!chromeApi?.runtime?.onMessage?.addListener) {
    throw new Error('chrome.runtime.onMessage is unavailable');
  }
  if (!chromeApi?.storage?.session) throw new Error('chrome.storage.session is unavailable');

  const sessionStorage = chromeApi.storage.session;
  let registered = false;
  let contextQueue = Promise.resolve();
  let generation = 0;
  const accountListeners = new Set();
  const authorityListeners = new Set();
  const invalidateAuthority = () => { for (const listener of authorityListeners) listener(); };
  const notifyAccount = (invalidates = true) => {
    if (invalidates) invalidateAuthority();
    for (const listener of accountListeners) listener();
  };
  const observedDocuments = new Map();
  const remembered = new Map();
  const withdrawn = new Map();
  const presentationFences = new Map();
  const fencedTabs = new Set();
  let presentationConsentEpoch = currentConsent()?.consent_epoch;
  const finishPresentationFence = (tabId) => {
    const fence = presentationFences.get(tabId);
    if (!fence) return;
    clearTimeout(fence.timer);
    presentationFences.delete(tabId);
    fence.resolve();
  };
  const clearPresentation = () => {
    remembered.clear();
    for (const tabId of presentationFences.keys()) finishPresentationFence(tabId);
  };
  const syncPresentationConsent = () => {
    const epoch = currentConsent()?.consent_epoch;
    if (epoch !== presentationConsentEpoch) clearPresentation();
    presentationConsentEpoch = epoch;
  };
  const remember = (context) => {
    if (context.observed_platform_id === null || context.identity_conflict === true) return;
    remembered.set(context.tab_id, { senderKey: context.sender_key,
      documentId: context.document_id,
      accountId: context.observed_platform_id, pageEpoch: context.page_epoch,
      incognito: context.incognito === true, ...profileLabels(context), proven: true });
    if (remembered.size > MAX_CONTEXTS) {
      const oldest = remembered.keys().next().value;
      remembered.delete(oldest);
      finishPresentationFence(oldest);
    }
  };
  let persistedContextTabs = null;
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
    syncPresentationConsent();
    const stored = await storageGet(sessionStorage, PROVISIONING_IDENTITY_STORAGE_KEY, chromeApi);
    const contexts = normalizeContexts(stored[PROVISIONING_IDENTITY_STORAGE_KEY]);
    persistedContextTabs = new Set(contexts.map((context) => context.tab_id));
    const current = contexts.filter((context) => context.consent_epoch === currentConsent()?.consent_epoch);
    for (const context of current) if (context.identity_conflict === true) withdrawn.set(context.sender_key, context);
    return current;
  };
  const publishContexts = async (contexts, previous, invalidates = true) => {
    const document = storedDocument(contexts);
    await storageSet(sessionStorage, {
      [PROVISIONING_IDENTITY_STORAGE_KEY]: document,
    }, chromeApi);
    persistedContextTabs = new Set(document.contexts.map((context) => context.tab_id));
    // The early notification fences work immediately. Readers also need an
    // event after persistence so they cannot keep the previous account until
    // an unrelated tab event happens to wake them.
    const signature = (values) => JSON.stringify(values.map((context) => JSON.stringify([
      context.sender_key, context.tab_id, context.document_id, context.page_epoch,
      context.observed_platform_id, context.consent_epoch, context.identity_conflict === true,
      context.display_name, context.username,
      context.incognito === true,
    ])).sort());
    if (signature(document.contexts) !== signature(previous)) notifyAccount(invalidates);
  };

  const isWithdrawn = (senderKey, pageEpoch) => withdrawn.get(senderKey)?.page_epoch === pageEpoch;
  const withdrawOtherTabs = (sourceTab, incognito, accountId, contexts = []) => {
    const candidates = new Map(contexts.map(context => [context.tab_id, context]));
    for (const [tabId, last] of remembered) candidates.set(tabId, {
      tab_id: tabId, sender_key: last.senderKey, document_id: last.documentId,
      page_epoch: last.pageEpoch, observed_platform_id: last.accountId, incognito: last.incognito,
    });
    for (const [tabId, observed] of observedDocuments) {
      if (observed.accountId !== null) candidates.set(tabId, {
        tab_id: tabId, sender_key: observed.senderKey, document_id: observed.documentId,
        page_epoch: observed.pageEpoch, observed_platform_id: observed.accountId, incognito: observed.incognito,
      });
    }
    const removed = new Set();
    for (const context of candidates.values()) {
      if (context.tab_id === sourceTab || (context.incognito === true) !== incognito
        || context.observed_platform_id === null || context.observed_platform_id === accountId) continue;
      removed.add(context.tab_id);
      if (isWithdrawn(context.sender_key, context.page_epoch)) {
        withdrawn.get(context.sender_key).consent_epoch = currentConsent()?.consent_epoch;
        continue;
      }
      withdrawn.set(context.sender_key, { sender_key: context.sender_key, tab_id: context.tab_id,
        document_id: context.document_id, page_epoch: context.page_epoch, observed_platform_id: null,
        consent_epoch: currentConsent()?.consent_epoch, identity_conflict: true,
        ...(incognito ? { incognito: true } : {}) });
      if (withdrawn.size > MAX_CONTEXTS) withdrawn.delete(withdrawn.keys().next().value);
      invalidateDocument(context.tab_id);
      remembered.delete(context.tab_id); observedDocuments.delete(context.tab_id);
      finishPresentationFence(context.tab_id);
      void chromeApi.tabs?.sendMessage?.(context.tab_id, {
        type: PAGE_CONTROL_MESSAGE_TYPE, version: PAGE_CONTROL_VERSION, action: 'withdraw_identity',
      }, { documentId: context.document_id }).catch(() => undefined);
    }
    return removed;
  };

  const internalListener = (message, sender, sendResponse) => {
    if (![PROVISIONING_IDENTITY_MESSAGE_TYPE, PROVISIONING_IDENTITY_RESET_TYPE].includes(message?.type)) return false;
    const update = identityUpdate(message);
    if (update === undefined) return false;
    let senderKey;
    try {
      senderKey = captureSenderKey(sender, chromeApi.runtime.id);
    } catch (_error) {
      return false;
    }
    if (isWithdrawn(senderKey, update.pageEpoch)) { sendResponse({ ok: false }); return false; }
    // Fence admitted captures before waiting for persistence or readiness.
    invalidateDocument(sender.tab.id);
    syncPresentationConsent();
    const last = remembered.get(sender.tab.id);
    const authorityChanged = message.type === PROVISIONING_IDENTITY_RESET_TYPE || !last
      || last.senderKey !== senderKey || last.accountId !== update.accountId;
    if (last && (last.senderKey !== senderKey || message.type !== PROVISIONING_IDENTITY_RESET_TYPE
      && update.accountId !== last.accountId)) remembered.delete(sender.tab.id);
    else if (last && message.type === PROVISIONING_IDENTITY_RESET_TYPE) last.proven = true;
    const previous = observedDocuments.get(sender.tab.id);
    const observed = { senderKey, documentId: sender.documentId, pageEpoch: update.pageEpoch,
      accountId: update.accountId, incognito: sender.tab.incognito === true };
    const presentationFence = presentationFences.get(sender.tab.id);
    observedDocuments.set(sender.tab.id, observed);
    if (message.type !== PROVISIONING_IDENTITY_RESET_TYPE) {
      withdrawOtherTabs(sender.tab.id, observed.incognito, update.accountId);
    }
    if (!previous || previous.senderKey !== observed.senderKey
      || previous.pageEpoch !== observed.pageEpoch || previous.accountId !== observed.accountId) notifyAccount(authorityChanged);
    const admittedGeneration = generation;
    const admittedConsentEpoch = currentConsent()?.consent_epoch;
    void (async () => {
      await ensureReady();
      if (!allowsIdentity() || (admittedConsentEpoch !== undefined
        && admittedConsentEpoch !== currentConsent()?.consent_epoch)) return { ok: false };
      return serializeContext(async () => {
        if (generation !== admittedGeneration || !allowsIdentity()
          || observedDocuments.get(sender.tab.id) !== observed) return { ok: false };
        const contexts = await loadContexts();
        if (isWithdrawn(senderKey, update.pageEpoch)) return { ok: false };
        if (message.type === PROVISIONING_IDENTITY_RESET_TYPE && withdrawn.has(senderKey)) {
          fencedTabs.delete(sender.tab.id);
          finishPresentationFence(sender.tab.id);
          return { ok: true };
        }
        const removed = message.type === PROVISIONING_IDENTITY_RESET_TYPE ? new Set()
          : withdrawOtherTabs(sender.tab.id, observed.incognito, update.accountId, contexts);
        if (message.type === PROVISIONING_IDENTITY_RESET_TYPE && !remembered.has(sender.tab.id)) {
          const previousContext = contexts.find(context => context.sender_key === senderKey);
          if (previousContext) {
            remember(previousContext);
            const last = remembered.get(sender.tab.id);
            if (last) last.proven = observedDocuments.get(sender.tab.id) === observed
              && presentationFences.get(sender.tab.id) === presentationFence;
          }
        }
        const retained = contexts.filter((context) => context.tab_id !== sender.tab.id && !removed.has(context.tab_id)
          && !isWithdrawn(context.sender_key, context.page_epoch));
        for (const context of withdrawn.values()) {
          if (context.tab_id !== sender.tab.id && context.consent_epoch === currentConsent()?.consent_epoch
            && !retained.some(value => value.tab_id === context.tab_id)) retained.push(context);
        }
        retained.push({
          sender_key: senderKey, tab_id: sender.tab.id, document_id: sender.documentId,
          page_epoch: update.pageEpoch, observed_platform_id: update.accountId,
          consent_epoch: currentConsent().consent_epoch,
          ...(observed.incognito ? { incognito: true } : {}),
          ...profileLabels(update),
        });
        await publishContexts(retained, contexts, authorityChanged);
        if (observedDocuments.get(sender.tab.id) === observed) {
          fencedTabs.delete(sender.tab.id);
          if (message.type !== PROVISIONING_IDENTITY_RESET_TYPE) remember(retained.at(-1));
          if (presentationFences.get(sender.tab.id) === presentationFence) finishPresentationFence(sender.tab.id);
        }
        return { ok: true };
      });
    })().then(sendResponse, () => sendResponse({ ok: false }));
    return true;
  };

  const contextForSender = async (sender) => {
    const senderKey = captureSenderKey(sender, chromeApi.runtime.id);
    const contexts = await loadContexts();
    const context = contexts.find((candidate) => candidate.sender_key === senderKey);
    const observed = observedDocuments.get(sender.tab.id);
    return context === undefined || context.identity_conflict === true || fencedTabs.has(sender.tab.id)
      || observed && (observed.senderKey !== senderKey || observed.pageEpoch !== context.page_epoch
        || observed.accountId !== context.observed_platform_id)
      || isWithdrawn(senderKey, context.page_epoch) ? null : structuredClone(context);
  };

  const clearContexts = () => {
    generation += 1;
    documentGenerations.clear();
    observedDocuments.clear();
    withdrawn.clear();
    fencedTabs.clear();
    clearPresentation();
    persistedContextTabs = null;
    notifyAccount();
    return serializeContext(async () => {
      const previous = await loadContexts();
      withdrawn.clear();
      return publishContexts([], previous);
    });
  };
  const refreshIdentity = (tabId, fence) => {
    fence.refreshSent = true;
    void (async () => {
      await ensureReady();
      if (!allowsIdentity()) return;
      await chromeApi.tabs?.sendMessage?.(tabId, {
        type: PAGE_CONTROL_MESSAGE_TYPE, version: PAGE_CONTROL_VERSION, action: 'refresh_identity',
      }, { frameId: 0 });
    })().catch(() => {
      if (presentationFences.get(tabId) === fence) { finishPresentationFence(tabId); invalidateAuthority(); }
    });
  };
  const removeTab = (tabId, fenced = false) => {
    syncPresentationConsent();
    finishPresentationFence(tabId);
    let fence;
    if (fenced) {
      fencedTabs.add(tabId);
      const last = remembered.get(tabId);
      if (last) last.proven = false;
      let resolve;
      const promise = new Promise(done => { resolve = done; });
      fence = { promise, resolve, timer: setTimeout(() => {
        if (presentationFences.get(tabId) === fence) { finishPresentationFence(tabId); invalidateAuthority(); }
      }, PRESENTATION_PROOF_TIMEOUT_MS) };
      presentationFences.set(tabId, fence);
    } else {
      remembered.delete(tabId); fencedTabs.delete(tabId);
      for (const [key, context] of withdrawn) if (context.tab_id === tabId) withdrawn.delete(key);
    }
    const affectsIdentity = persistedContextTabs === null || persistedContextTabs.has(tabId)
      || observedDocuments.has(tabId);
    invalidateDocument(tabId);
    observedDocuments.delete(tabId);
    // Unknown restored membership remains conservative. Once loaded, lifecycle
    // changes to unrelated setup tabs cannot invalidate the creator context.
    if (affectsIdentity) notifyAccount(!fenced);
    void (async () => {
      // A tab lifecycle event can be the event that wakes a cold MV3 worker.
      // Wait until persisted consent has been restored before filtering durable
      // session contexts by consent_epoch, otherwise the controller's fresh
      // default epoch can erase valid contexts from the previous worker.
      await ensureReady();
      return serializeContext(async () => {
        const contexts = await loadContexts();
        if (!fenced) for (const [key, context] of withdrawn) if (context.tab_id === tabId) withdrawn.delete(key);
        if (fenced && presentationFences.get(tabId) === fence) {
          if (!remembered.has(tabId)) {
            const previous = contexts.find(context => context.tab_id === tabId);
            if (previous) remember(previous);
          }
          const last = remembered.get(tabId);
          if (last) {
            if (!fence.refreshSent) { last.proven = false; refreshIdentity(tabId, fence); }
          } else finishPresentationFence(tabId);
        }
        const retained = contexts.filter((context) => context.tab_id !== tabId);
        await publishContexts(retained, contexts, !fenced);
      });
    })().catch(() => undefined);
    if (fenced && remembered.has(tabId)) refreshIdentity(tabId, fence);
  };
  const tabRemoved = (tabId) => removeTab(tabId);
  const tabUpdated = (tabId, changeInfo) => {
    if (changeInfo.status === 'loading' || changeInfo.url !== undefined) removeTab(tabId, true);
    if (changeInfo.status === 'complete') {
      // Browser loading events can also occur without replacing the top-level
      // document. Re-observe through its current content bridge after clearing;
      // never restore a cached identity from a previous document.
      void (async () => {
        await ensureReady();
        await contextQueue;
        if (!allowsIdentity()) return;
        await chromeApi.tabs?.sendMessage?.(tabId, {
          type: PAGE_CONTROL_MESSAGE_TYPE,
          version: PAGE_CONTROL_VERSION,
          action: 'refresh_identity',
        }, { frameId: 0 });
      })().catch(() => undefined);
    }
  };
  const resetSession = () => { void clearContexts().catch(() => undefined); };

  const presentedContexts = async () => {
    for (;;) {
      syncPresentationConsent();
      // Wait outside the capture queue so a post-fence reset can prove its document.
      while (presentationFences.size) await Promise.all([...presentationFences.values()].map(fence => fence.promise));
      const presented = await serializeContext(async () => {
        const contexts = await loadContexts();
        return contexts.map(context => {
          const last = remembered.get(context.tab_id);
          if (context.observed_platform_id === null && last?.proven && last.senderKey === context.sender_key) {
            return { ...context, observed_platform_id: last.accountId, ...profileLabels(last) };
          }
          const observed = observedDocuments.get(context.tab_id);
          return observed && observed.senderKey !== context.sender_key
            ? { ...context, observed_platform_id: null } : context;
        });
      });
      if (!presentationFences.size) return presented;
    }
  };

  const confirmedContexts = async () => {
    while (presentationFences.size) await Promise.all([...presentationFences.values()].map(fence => fence.promise));
    const contexts = await serializeContext(loadContexts);
    if (presentationFences.size) return confirmedContexts();
    return contexts.map(context => fencedTabs.has(context.tab_id)
      || isWithdrawn(context.sender_key, context.page_epoch) ? { ...context, observed_platform_id: null } : context);
  };

  return Object.freeze({
    async currentAccountId() { return provisioningAccountId(await confirmedContexts()); },
    async currentProfile() {
      const contexts = await confirmedContexts();
      const accountId = provisioningAccountId(contexts);
      if (accountId === null) return null;
      const observed = contexts.findLast(context => context.observed_platform_id === accountId);
      return { creator_account_id: accountId, ...profileLabels(observed) };
    },
    async presentedAccountId() { return provisioningAccountId(await presentedContexts()); },
    async presentedProfile() {
      const contexts = await presentedContexts();
      const accountId = provisioningAccountId(contexts);
      if (accountId === null) return null;
      return { creator_account_id: accountId,
        ...profileLabels(contexts.findLast(context => context.observed_platform_id === accountId)) };
    },
    onAccountChange(listener) { accountListeners.add(listener); return () => accountListeners.delete(listener); },
    onAuthorityInvalidated(listener) { authorityListeners.add(listener); return () => authorityListeners.delete(listener); },
    clearContexts,
    contextFor(sender) {
      return serializeContext(() => contextForSender(sender));
    },
    withCaptureContext(sender, work) {
      if (typeof work !== 'function') throw new TypeError('Capture context work is required');
      return serializeContext(async () => {
        const snapshot = { generation, documentGeneration: documentGenerations.get(sender.tab.id) ?? 0,
          consentEpoch: currentConsent()?.consent_epoch };
        return { ...snapshot, context: await contextForSender(sender) };
      }).then((snapshot) => work(snapshot.context, () => {
        if (snapshot.generation !== generation
          || snapshot.consentEpoch !== currentConsent()?.consent_epoch
          || snapshot.documentGeneration !== (documentGenerations.get(sender.tab.id) ?? 0)) {
          throw Object.assign(new Error('stale_capture_context'), { code: 'stale_capture_context', retryable: false });
        }
      }));
    },
    register() {
      if (registered) return;
      chromeApi.runtime.onMessage.addListener(internalListener);
      chromeApi.tabs?.onRemoved?.addListener(tabRemoved);
      chromeApi.tabs?.onUpdated?.addListener(tabUpdated);
      chromeApi.runtime.onStartup?.addListener(resetSession);
      chromeApi.runtime.onInstalled?.addListener(resetSession);
      registered = true;
    },
    unregister() {
      if (!registered) return;
      chromeApi.runtime.onMessage.removeListener?.(internalListener);
      chromeApi.tabs?.onRemoved?.removeListener?.(tabRemoved);
      chromeApi.tabs?.onUpdated?.removeListener?.(tabUpdated);
      chromeApi.runtime.onStartup?.removeListener?.(resetSession);
      chromeApi.runtime.onInstalled?.removeListener?.(resetSession);
      registered = false;
    },
  });
}
