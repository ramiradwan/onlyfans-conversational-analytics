import { createOnboardingWorkspace } from './onboarding-workspace.mjs';
import { legalReleaseBindings } from './legal-release-bindings.mjs';
import { setupTransferReceiverUrl } from './onboarding-release-config.mjs';
import { FULL_REVIEW_INTENT_KEY, fullReviewIntent } from './onboarding-full-intent.mjs';
import { attachInstalledHelpers } from './browser-task-owner.mjs';
import { BROWSER_TASK_ADOPTION_KEY, BROWSER_TASK_INSTANCE_KEY, createBrowserTaskEntry, desktopAdopted } from './browser-task-entry.mjs';
export const WORKSPACE_MESSAGE_TYPE = 'ofca.workspace.v1';
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
export function productionWorkspaceRoutes(chromeApi) {
  return { extension: chromeApi.runtime.getURL('setup.html'), hosted: setupTransferReceiverUrl };
}
export function registerOnboardingWorkspace({ chromeApi, consentController, identityBridge,
  routes = productionWorkspaceRoutes(chromeApi) }) {
  const workspace = createOnboardingWorkspace({ chromeApi, routes });
  const browserTaskEntry = createBrowserTaskEntry({ storage: chromeApi.storage.session });
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
        if (revision === identityRevision) return revision;
      }
    });
    identityQueue = operation.catch(() => undefined);
    return operation;
  }
  identityBridge.onAccountChange?.(() => { identityRevision++; void reconcileAccount().catch(() => undefined); });
  async function open({ section = '' } = {}) {
    await reconcileAccount();
    const draft_scope = await scope();
    let record = await workspace.read();
    if (!record) {
      await workspace.resumeExisting({ draft_scope, route: 'extension' });
      record = await workspace.read();
    }
    const result = await workspace.open({ journey_id: record?.journey_id ?? crypto.randomUUID(),
      route: 'extension', explicit: true, draft_scope });
    await reconcileAccount();
    if (['full', 'desktop'].includes(section)) await chromeApi.storage.session.set({
      [FULL_REVIEW_INTENT_KEY]: fullReviewIntent(result.journey_id, (await workspace.read()).draft_scope),
    });
    return result;
  }
  const listener = (message, sender, reply) => {
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
      if (exact(message, ['type', 'action']) && message.action === 'browser_task') return browserTaskEntry.read(admitted.record);
      if (exact(message, ['type', 'action']) && message.action === 'dismiss_browser_task') {
        const current = await browserTaskEntry.read(admitted.record);
        if (current) await browserTaskEntry.close(current.task);
        return { dismissed: true };
      }
      if (exact(message, ['type', 'action']) && message.action === 'desktop_adoption') {
        const saved = await chromeApi.storage.local.get([BROWSER_TASK_ADOPTION_KEY, BROWSER_TASK_INSTANCE_KEY]);
        return { adopted: desktopAdopted(saved[BROWSER_TASK_ADOPTION_KEY], admitted.record, saved[BROWSER_TASK_INSTANCE_KEY]) };
      }
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
  chromeApi.runtime.onMessageExternal?.addListener(listener);
  chromeApi.action?.onClicked?.addListener(() => { void open().catch(() => undefined); });
  chromeApi.runtime.onInstalled?.addListener(({ reason }) => {
    if (reason !== 'install') return;
    void attachInstalledHelpers(chromeApi).then(attached => attached ? null
      : reconcileAccount().then(scope).then(draft_scope => workspace.resumeExisting({ draft_scope, route: 'extension' }))).catch(() => undefined);
  });
  return Object.freeze({ workspace, open,
    async browserTaskContext(journeyId, { adopt = false, canAdopt = () => false, authorize = async () => {} } = {}) {
      await reconcileAccount();
      let record = await workspace.read();
      if (adopt) {
        await workspace.adoptBrowserTask({ journey_id: journeyId, draft_scope: await scope() }, canAdopt, authorize);
        await reconcileAccount();
        record = await workspace.read();
      }
      if (record && record.journey_id !== journeyId) throw Error('task_changed');
      return { record, draft_scope: record?.draft_scope ?? await scope() };
    },
    closeBrowserTask(task) { return browserTaskEntry.close(task); },
    async openBrowserTask(task, draftScope, authorize) {
      await authorize();
      const journeyId = task.journey_id;
      const record = await workspace.read();
      if (record && record.journey_id !== journeyId) throw Error('task_changed');
      await workspace.open({ journey_id: journeyId, route: 'extension', explicit: true, draft_scope: record?.draft_scope ?? draftScope });
      await reconcileAccount();
      const current = await workspace.read();
      if (current.draft_scope.scope_id !== draftScope.scope_id
        || current.draft_scope.disclosure_bundle_id !== draftScope.disclosure_bundle_id) throw Error('task_changed');
      await browserTaskEntry.open(task, current.draft_scope, authorize);
      await chromeApi.storage.session.set({ [FULL_REVIEW_INTENT_KEY]: fullReviewIntent(journeyId, current.draft_scope) });
    },
  });
}
