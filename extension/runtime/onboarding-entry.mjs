import { createOnboardingWorkspace } from './onboarding-workspace.mjs';
import { LOCAL_SERVICE_ORIGIN } from '../transport/local-service-endpoints.mjs';
import { legalReleaseBindings } from './legal-release-bindings.mjs';
import { onboardingHostedOrigin } from './onboarding-release-config.mjs';
import { FULL_REVIEW_INTENT_KEY, fullReviewIntent } from './onboarding-full-intent.mjs';
import { NATIVE_RETURN_TYPE, NATIVE_DISCOVER_TYPE, NATIVE_FOCUS_TYPE } from './onboarding-native-launch.mjs';

export const WORKSPACE_MESSAGE_TYPE = 'ofca.workspace.v1';
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
export function productionWorkspaceRoutes(chromeApi) {
  return { extension: chromeApi.runtime.getURL('setup.html'), hosted: onboardingHostedOrigin === null ? null : `${onboardingHostedOrigin}/public/onboarding`,
    provisioning: `${LOCAL_SERVICE_ORIGIN}/provisioning`, bridge: `${LOCAL_SERVICE_ORIGIN}/` };
}
export function registerOnboardingWorkspace({ chromeApi, consentController, identityBridge }) {
  const workspace = createOnboardingWorkspace({ chromeApi, routes: productionWorkspaceRoutes(chromeApi) });
  let scopePromise = null, identityRevision = 0;
  let identityQueue = Promise.resolve();
  async function scope() {
    if (scopePromise) return scopePromise;
    scopePromise = (async () => {
      const record = await workspace.read();
      const source = JSON.stringify(legalReleaseBindings()?.instruments ?? null);
      const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(source)));
      const disclosure_bundle_id = [...digest].map((value) => value.toString(16).padStart(2, '0')).join('');
      const current = { scope_id: record?.draft_scope.disclosure_bundle_id === disclosure_bundle_id
        ? record.draft_scope.scope_id : crypto.randomUUID(), disclosure_bundle_id };
      if (record && record.draft_scope.disclosure_bundle_id !== disclosure_bundle_id) await workspace.refreshScope(current);
      return current;
    })().finally(() => { scopePromise = null; });
    return scopePromise;
  }
  function reconcileAccount() {
    const operation = identityQueue.then(async () => {
      await consentController.initialize();
      for (;;) {
        const revision = identityRevision;
        const observed = await identityBridge.currentAccountId();
        if (observed !== null && (typeof observed !== 'string' || observed.length < 1 || observed.length > 200)) {
          throw Error('workspace_identity_unavailable');
        }
        const account_digest = observed === null ? null : [...new Uint8Array(await crypto.subtle.digest('SHA-256',
          new TextEncoder().encode(JSON.stringify(['onboarding-draft-account.v1', observed]))))]
          .map((value) => value.toString(16).padStart(2, '0')).join('');
        if (revision !== identityRevision) continue;
        await scope();
        await workspace.reconcileIdentity({ account_digest });
        if (revision === identityRevision) return;
      }
    });
    identityQueue = operation.catch(() => undefined);
    return operation;
  }
  identityBridge.onAccountChange?.(() => { identityRevision++; void reconcileAccount().catch(() => undefined); });
  async function open({ section = '', anchorTab = null } = {}) {
    await reconcileAccount();
    const draft_scope = await scope();
    // An exact existing setup tab wins, including a page open before installation.
    let record = await workspace.read();
    if (!record) {
      await workspace.resumeExisting({ draft_scope, route: 'extension' });
      record = await workspace.read();
    }
    const result = await workspace.open({ journey_id: record?.journey_id ?? crypto.randomUUID(), route: 'extension', explicit: true, draft_scope });
    await reconcileAccount();
    const currentScope = (await workspace.read()).draft_scope;
    // Intent is UI only and does not grant Full consent or start pairing.
    if (['full', 'desktop'].includes(section)) await chromeApi.storage.session.set({
      [FULL_REVIEW_INTENT_KEY]: fullReviewIntent(result.journey_id, currentScope),
    });
    if (anchorTab && result.tab_id !== anchorTab.id) {
      // Legacy desktop callers may not have a journey reference. They keep the
      // supported route; no arbitrary existing page can be adopted or navigated.
    }
    return result;
  }
  const listener = (message, sender, reply) => {
    if ([NATIVE_RETURN_TYPE, NATIVE_DISCOVER_TYPE, NATIVE_FOCUS_TYPE].includes(message?.type)) {
      if (!exact(message, message.type === NATIVE_RETURN_TYPE ? ['type', 'journey_id', 'route'] : ['type'])) {
        reply({ ok: false, code: 'return_unavailable' }); return false;
      }
      const operation = reconcileAccount().then(() => message.type === NATIVE_DISCOVER_TYPE ? workspace.discoverNativeLaunch(sender)
        : message.type === NATIVE_FOCUS_TYPE ? workspace.focusFromNative(sender)
          : workspace.returnFromNative(sender, { journey_id: message.journey_id, route: message.route }));
      void operation
        .then((result) => reply({ ok: true, result }), (error) => reply({ ok: false,
          code: ['no_workspace', 'workspace_exists'].includes(error?.message) ? error.message : 'return_unavailable' }));
      return true;
    }
    if (message?.type !== WORKSPACE_MESSAGE_TYPE) return false;
    const run = async () => {
      await reconcileAccount();
      const admitted = await workspace.admit(sender);
      if (exact(message, ['type', 'action']) && message.action === 'read') {
        if (admitted.route === 'hosted') throw Error('workspace_local_only');
        return admitted.record;
      }
      if (exact(message, ['type', 'action', 'route']) && message.action === 'navigate') return workspace.navigate(sender, { route: message.route });
      if (admitted.route !== 'extension') throw Error('workspace_draft_owner');
      if (exact(message, ['type', 'action']) && message.action === 'prepare_launch') return workspace.prepareNativeLaunch(sender);
      if (exact(message, ['type', 'action', 'scope_id', 'draft']) && message.action === 'draft') {
        return workspace.saveDraft(sender, { scope_id: message.scope_id, draft: message.draft });
      }
      throw Error('workspace_request_invalid');
    };
    void run().then((result) => reply({ ok: true, result }), () => reply({ ok: false, code: 'workspace_unavailable' }));
    return true;
  };
  chromeApi.runtime.onMessage.addListener(listener);
  chromeApi.runtime.onConnect?.addListener((port) => {
    if (port.name === 'ofca.workspace.navigation.v1') void workspace.bindNavigationPort(port);
  });
  chromeApi.runtime.onMessageExternal?.addListener(listener);
  chromeApi.action?.onClicked?.addListener(() => { void open().catch(() => undefined); });
  chromeApi.runtime.onInstalled?.addListener(({ reason }) => {
    if (reason !== 'install') return;
    void reconcileAccount().then(scope).then((draft_scope) => workspace.resumeExisting({ draft_scope, route: 'extension' })).catch(() => undefined);
  });
  return Object.freeze({ workspace, open });
}
