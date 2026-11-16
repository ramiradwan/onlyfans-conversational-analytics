import { createProvisioningIdentityBridge } from '../transport/provisioning-identity.mjs';
import { registerOnboardingWorkspace } from '../runtime/onboarding-entry.mjs';
import { createBrowserTaskOwner } from '../runtime/browser-task-owner.mjs';

export const tick = () => new Promise(resolve => setImmediate(resolve));
export const event = () => { const listeners = new Set(); return {
  listeners, addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
  emit: (...args) => { for (const fn of listeners) fn(...args); },
}; };
export function identityNavigation(chromeApi = {}) {
  const local = {}, session = {}, tabs = [], refreshes = [];
  const area = values => ({ async get(keys, callback) {
    const result = structuredClone(keys === undefined ? values : Object.fromEntries(
      (Array.isArray(keys) ? keys : [keys]).map(key => [key, values[key]])));
    callback?.(result); return result;
  }, async set(update, callback) { Object.assign(values, structuredClone(update)); callback?.(); },
  async remove(key) { delete values[key]; } });
  chromeApi.storage = { local: area(local), session: area(session) };
  chromeApi.runtime ??= { id: 'a'.repeat(32) };
  Object.assign(chromeApi.runtime, { onMessage: event(), onStartup: event(), onInstalled: event(),
    getURL: path => `chrome-extension://${chromeApi.runtime.id}/${path}`,
    async getContexts() { return tabs.map(tab => ({ tabId: tab.id, windowId: tab.windowId,
      frameId: 0, documentId: tab.documentId, documentUrl: tab.url })); } });
  const originalGet = chromeApi.tabs?.get;
  chromeApi.tabs ??= {};
  Object.assign(chromeApi.tabs, { onUpdated: event(), onRemoved: event(),
    async query() { return structuredClone(tabs); },
    async get(id) { return structuredClone(tabs.find(tab => tab.id === id)) ?? originalGet?.(id); },
    async create(options) { const tab = { id: 7, windowId: 1, documentId: 'setup-document', ...options };
      tabs.push(tab); return structuredClone(tab); },
    async sendMessage(...args) { refreshes.push(args); await h.refresh?.(); },
  });
  chromeApi.windows ??= { async update() {} };
  const consent = { mode: 'full', consent_epoch: '10000000-0000-4000-8000-000000000008' };
  const consentController = { async initialize() {}, async status() {
    return { consent: structuredClone(consent), onlyfans_permission: true }; } };
  const identity = createProvisioningIdentityBridge({ chromeApi, currentConsent: () => consent });
  identity.register();
  const onboarding = registerOnboardingWorkspace({ chromeApi, consentController, identityBridge: identity });
  const owner = createBrowserTaskOwner({ chromeApi, onboarding, consentController, identityBridge: identity,
    legalActivationController: { async status() { return { configured: true, requires_reauthorization: false }; } },
    openCreatorAccount: () => { throw Error('unexpected account action'); } });
  const sender = { id: chromeApi.runtime.id, frameId: 0, tab: { id: 17 }, documentId: 'platform-document',
    url: 'https://onlyfans.com/my/chats', documentLifecycle: 'active' };
  const dispatch = (message, source = sender) => new Promise(resolve => {
    for (const fn of chromeApi.runtime.onMessage.listeners) if (fn(message, source, resolve) === true) return;
    resolve(undefined);
  });
  let page = 0;
  const pageEpoch = () => `10000000-0000-4000-8000-${String(++page).padStart(12, '0')}`;
  const h = { chromeApi, local, session, tabs, consent, identity, onboarding, owner, sender, dispatch, refreshes,
    confirm(account = 'synthetic-creator', source = sender) { return dispatch({ type: 'ofca.provisioning.identity.update',
      version: 1, page_epoch: pageEpoch(), authenticated_profile: account === null ? null : {
        creator_account_id: account, display_name: 'Synthetic Creator', username: 'synthetic_handle' } }, source); },
    reset(source = sender) { return dispatch({ type: 'ofca.provisioning.identity.reset', version: 1, page_epoch: pageEpoch() }, source); },
    fence() { chromeApi.tabs.onUpdated.emit(17, { url: 'https://onlyfans.com/my/profile' }); },
    async navigate(order) {
      h.refresh = () => h.reset();
      if (order === 'reset-first') await h.reset();
      h.fence();
      if (order === 'url-first') await h.reset();
      await tick(); await tick();
    },
  };
  return h;
}
