import assert from 'node:assert/strict';
import test from 'node:test';
import { registerOnboardingWorkspace } from '../runtime/onboarding-entry.mjs';
import { createProvisioningIdentityBridge, PROVISIONING_IDENTITY_STORAGE_KEY,
  PROVISIONING_IDENTITY_STORAGE_SCHEMA } from '../transport/provisioning-identity.mjs';

const epoch = '10000000-0000-4000-8000-000000000008';
const stateKey = 'onboarding_workspace_state_v3';
const checked = { terms_checked: true, risk_checked: true, full_checked: true };
function event() {
  const listeners = new Set();
  return { listeners, addListener: (fn) => listeners.add(fn), removeListener: (fn) => listeners.delete(fn),
    emit: (...args) => { for (const fn of listeners) fn(...args); } };
}
function storage(values) {
  return {
    async get(keys, callback) {
      const result = structuredClone(Object.fromEntries((Array.isArray(keys) ? keys : [keys])
        .filter((key) => Object.hasOwn(values, key)).map((key) => [key, values[key]])));
      callback?.(result); return result;
    },
    async set(update, callback) { Object.assign(values, structuredClone(update)); callback?.(); },
  };
}
const dispatch = (event, message, sender) => new Promise((resolve) => {
  for (const listener of event.listeners) if (listener(message, sender, resolve) === true) return;
  resolve(undefined);
});

for (const changedTab of ['setup', 'platform']) test(`real identity bridge ${changedTab} lifecycle during desktop launch`, async () => {
  const local = {}, session = { [PROVISIONING_IDENTITY_STORAGE_KEY]: {
    schema: PROVISIONING_IDENTITY_STORAGE_SCHEMA,
    contexts: [{ sender_key: '17:platform-document', tab_id: 17, document_id: 'platform-document',
      page_epoch: '10000000-0000-4000-8000-000000000001', observed_platform_id: 'creator-a', consent_epoch: epoch }],
  } };
  const tabs = [], updates = [], updated = event();
  const chromeApi = {
    storage: { local: storage(local), session: storage(session) },
    runtime: { id: 'synthetic', getURL: (file) => `chrome-extension://synthetic/${file}`,
      onMessage: event(), onMessageExternal: event(), onConnect: event(), onInstalled: event(), onStartup: event(),
      async getContexts() { return tabs.map((tab) => ({ tabId: tab.id, windowId: tab.windowId,
        frameId: 0, documentId: tab.documentId, documentUrl: tab.url })); } },
    tabs: { onUpdated: updated, onRemoved: event(), onActivated: event(),
      async query() { return structuredClone(tabs); },
      async get(id) { return structuredClone(tabs.find((tab) => tab.id === id)); },
      async create(options) {
        const tab = { id: 7, windowId: 1, documentId: 'setup-document', ...options };
        tabs.push(tab); return structuredClone(tab);
      },
      async update(id, options) { Object.assign(tabs.find((tab) => tab.id === id), options); updates.push(options); },
      async sendMessage() {},
    },
    windows: { onFocusChanged: event(), async get() { return { focused: true }; }, async update() {} },
    action: { onClicked: event() },
  };
  const identity = createProvisioningIdentityBridge({ chromeApi,
    currentConsent: () => ({ mode: 'full', consent_epoch: epoch }) });
  identity.register();
  const entry = registerOnboardingWorkspace({ chromeApi, identityBridge: identity,
    consentController: { async initialize() {} } });
  await entry.open();
  const owner = tabs[0];
  const internalSender = { id: 'synthetic', frameId: 0, tab: { id: owner.id },
    documentId: owner.documentId, url: owner.url };
  assert.equal((await dispatch(chromeApi.runtime.onMessage, { type: 'ofca.workspace.v1', action: 'draft',
    scope_id: local[stateKey].record.draft_scope.scope_id, draft: checked }, internalSender)).ok, true);
  const previous = structuredClone(local[stateKey].record);
  updated.emit(changedTab === 'setup' ? owner.id : 17, { status: 'loading' });
  await new Promise(resolve => setImmediate(resolve));
  const launched = await dispatch(chromeApi.runtime.onMessage,
    { type: 'ofca.workspace.v1', action: 'prepare_launch' }, internalSender);
  assert.equal(launched.ok, true);
  const current = local[stateKey].record;
  assert.deepEqual(session.onboarding_native_launch_v1.draft_scope, current.draft_scope);
  assert.equal(session.onboarding_native_launch_v1.phase, 'pending');
  assert.equal(session.onboarding_native_launch_v1.expires_at - session.onboarding_native_launch_v1.created_at, 600000);
  if (changedTab === 'setup') {
    assert.deepEqual(current, previous);
    assert.equal(await identity.currentAccountId(), 'creator-a');
  } else {
    assert.notEqual(current.draft_scope.scope_id, previous.draft_scope.scope_id);
    assert.equal(current.draft.full_checked, false);
    assert.equal(await identity.currentAccountId(), null);
  }
  assert.deepEqual(updates, [], 'identity lifecycle never dispatches browser navigation');
  identity.unregister();
});
