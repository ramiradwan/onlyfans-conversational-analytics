// Preview drafts, exact browser-task ownership, and explicit receiving only.
import { workspaceAppLink, NATIVE_LAUNCH_KEY, NATIVE_LAUNCH_TTL_MS } from './onboarding-native-launch.mjs';
import { createWorkspaceStore } from './onboarding-workspace-store.mjs';
export { WORKSPACE_STATE_KEY } from './onboarding-workspace-store.mjs';
export const WORKSPACE_RECORD_KEY = 'onboarding_workspace_v1';
export const WORKSPACE_TAB_KEY = 'onboarding_workspace_tab_v1';
export const WORKSPACE_ACTIVITY_KEY = 'onboarding_workspace_activity_v1';
export const WORKSPACE_IDENTITY_KEY = 'onboarding_workspace_identity_v1';
const MAX_IDLE_MS = 30 * 24 * 60 * 60 * 1000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const ROUTES = ['extension', 'hosted'];
const DRAFT_FIELDS = ['terms_checked', 'risk_checked', 'full_checked'];
const validScope = (scope) => exact(scope, ['scope_id', 'disclosure_bundle_id']) && UUID.test(scope.scope_id)
  && /^[0-9a-f]{64}$/u.test(scope.disclosure_bundle_id);
const blankDraft = () => Object.fromEntries(DRAFT_FIELDS.map((key) => [key, false]));
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, keys) => isObject(value) && Object.keys(value).length === keys.length
  && keys.every((key) => Object.hasOwn(value, key));
function fail(code) { throw new Error(code); }
const validRecord = record => exact(record, ['version', 'journey_id', 'route', 'draft_scope', 'draft']) && record.version === 1
  && UUID.test(record.journey_id) && ROUTES.includes(record.route) && validScope(record.draft_scope)
  && exact(record.draft, DRAFT_FIELDS) && DRAFT_FIELDS.every(key => typeof record.draft[key] === 'boolean');
const validIdentity = (identity, record) => exact(identity, ['version', 'journey_id', 'scope_id', 'disclosure_bundle_id', 'account_digest'])
  && identity.version === 1 && identity.journey_id === record.journey_id && identity.scope_id === record.draft_scope.scope_id
  && identity.disclosure_bundle_id === record.draft_scope.disclosure_bundle_id
  && (identity.account_digest === null || /^[0-9a-f]{64}$/u.test(identity.account_digest));
/** Build-time routes, never a URL supplied by an incoming command. */
export function createOnboardingWorkspace({ chromeApi = globalThis.chrome, routes, now = Date.now }) {
  if (!exact(routes, ROUTES)) fail('workspace_routes_invalid');
  const registered = new Map();
  for (const route of ROUTES) {
    if (route === 'hosted' && routes[route] === null) continue;
    const url = new URL(routes[route]);
    const extension = chromeApi.runtime.getURL('setup.html');
    if (url.username || url.password || url.search || url.hash || url.href !== routes[route]
      || (route === 'extension' ? url.href !== extension
        : !(url.protocol === 'https:' && url.pathname === '/public/onboarding/setup'))) {
      fail('workspace_routes_invalid');
    }
    if (registered.has(url.href)) fail('workspace_routes_invalid');
    registered.set(url.href, route);

  }
  let queue = Promise.resolve();
  const localStorage = createWorkspaceStore({ storage: chromeApi.storage.local,
    keys: { record: WORKSPACE_RECORD_KEY, identity: WORKSPACE_IDENTITY_KEY, activity: WORKSPACE_ACTIVITY_KEY },
    validRecord, validIdentity });
  const serialize = (work) => {
    const operation = queue.then(work);
    queue = operation.catch(() => undefined);
    return operation;
  };
  const reference = (route, journeyId) => {
    if (!ROUTES.includes(route) || !routes[route] || !UUID.test(journeyId)) fail('workspace_reference_invalid');
    return `${routes[route]}#journey=${journeyId}`;
  };
  function parse(value) {
    try {
      const url = new URL(value);
      const fragment = url.hash;
      url.hash = '';
      const route = registered.get(url.href);
      const journeyId = fragment.startsWith('#journey=') ? fragment.slice(9) : '';
      if (route && UUID.test(journeyId) && `${url.href}#journey=${journeyId}` === value) return { route, journeyId };
    } catch { /* Unregistered or malformed navigation is never adopted. */ }
    return null;
  }
  const saveRecord = (record) => localStorage.set({ [WORKSPACE_RECORD_KEY]: record, [WORKSPACE_ACTIVITY_KEY]: now() });
  const readRecord = async () => {
    const touched = (await localStorage.get(WORKSPACE_ACTIVITY_KEY))[WORKSPACE_ACTIVITY_KEY];
    if (Number.isFinite(touched) && (now() - touched > MAX_IDLE_MS || touched > now())) {
      await localStorage.remove([WORKSPACE_RECORD_KEY, WORKSPACE_ACTIVITY_KEY, WORKSPACE_IDENTITY_KEY]);
      return null;
    }
    const record = (await localStorage.get(WORKSPACE_RECORD_KEY))[WORKSPACE_RECORD_KEY];
    if (!exact(record, ['version', 'journey_id', 'route', 'draft_scope', 'draft']) || record.version !== 1
      || !UUID.test(record.journey_id) || !ROUTES.includes(record.route)
      || !validScope(record.draft_scope)
      || !exact(record.draft, DRAFT_FIELDS) || DRAFT_FIELDS.some((key) => typeof record.draft[key] !== 'boolean')) return null;
    return record;
  };
  const storeTab = (tab, journeyId) => chromeApi.storage.session.set({
    [WORKSPACE_TAB_KEY]: { version: 1, journey_id: journeyId, tab_id: tab.id, window_id: tab.windowId,
      ...(typeof tab.documentId === 'string' && tab.documentId ? { document_id: tab.documentId } : {}) },
  });
  const extensionContexts = async () => typeof chromeApi.runtime.getContexts === 'function'
    ? chromeApi.runtime.getContexts({ contextTypes: ['TAB'] }) : [];
  const getTab = async (id) => {
    const tab = await chromeApi.tabs.get(id);
    const context = (await extensionContexts()).find((value) => value.tabId === id && value.frameId === 0);
    // Always compare the current document, including reload to the same URL.
    if (context) return { ...tab, url: context.documentUrl, documentId: context.documentId };
    if (tab.url && parse(tab.url) && chromeApi.scripting?.executeScript) {
      const documents = await chromeApi.scripting.executeScript({ target: { tabId: id, frameIds: [0] },
        world: 'ISOLATED', func: () => null });
      const document = documents.find((value) => value.frameId === 0);
      if (!document?.documentId) fail('workspace_document_unavailable');
      return { ...tab, documentId: document.documentId };
    }
    return tab;
  };
  const registeredTabs = async () => {
    const origins = [...new Set([...registered.keys()].filter((value) => !value.startsWith('chrome-extension:')).map((value) => {
      const url = new URL(value); return `${url.protocol}//${url.hostname}/*`;
    }))];
    const tabs = await chromeApi.tabs.query({ url: origins });
    for (const context of await extensionContexts()) {
      if (context.frameId === 0 && context.tabId >= 0 && !tabs.some((tab) => tab.id === context.tabId)) {
        tabs.push({ id: context.tabId, windowId: context.windowId, url: context.documentUrl });
      }
    }
    return tabs.filter((tab) => parse(tab.url)).sort((a, b) => a.id - b.id);
  };
  async function liveTab(journeyId) {
    const stored = (await chromeApi.storage.session.get(WORKSPACE_TAB_KEY))[WORKSPACE_TAB_KEY];
    if (stored?.version === 1 && stored.journey_id === journeyId && Number.isInteger(stored.tab_id)) {
      try {
        const tab = await getTab(stored.tab_id);
        if (parse(tab.url)?.journeyId === journeyId) return tab;
      } catch { /* Closed tabs are recovered by an explicit open. */ }
    }
    // Requires access only to the registered first-party origins. A broad tabs
    // permission is unnecessary. Filter exact path and UUID again after query.
    return (await registeredTabs()).find((tab) => parse(tab.url).journeyId === journeyId) ?? null;
  }
  async function admitted(sender) {
    const parsed = parse(sender?.url);
    if (!parsed || (parsed.route === 'extension' ? sender.id !== chromeApi.runtime.id : sender.id !== undefined)) {
      fail('workspace_sender_invalid');
    }
    let tabId = sender?.tab?.id;
    if (parsed.route === 'extension' && tabId === undefined && typeof sender.documentId === 'string') {
      const contexts = await chromeApi.runtime.getContexts({ documentIds: [sender.documentId], contextTypes: ['TAB'] });
      const context = contexts.find((value) => value.documentUrl === sender.url && value.tabId >= 0 && value.frameId === 0);
      tabId = context?.tabId;
    }
    if ((sender.frameId !== undefined && sender.frameId !== 0) || !Number.isInteger(tabId)
      || (parsed.route !== 'extension' && sender.frameId !== 0)) fail('workspace_sender_invalid');
    const record = await readRecord();
    if (!record || record.journey_id !== parsed.journeyId) fail('workspace_not_registered');
    const tab = await getTab(tabId);
    if (tab.url !== sender.url) fail('workspace_sender_stale');
    if (tab.documentId && tab.documentId !== sender.documentId) fail('workspace_sender_stale');
    const owner = await liveTab(record.journey_id);
    if (owner?.id !== tab.id) fail('workspace_sender_not_owner');
    await storeTab(tab, record.journey_id);
    return { record, tab, parsed };
  }
  async function pendingLaunch() {
    const launch = (await chromeApi.storage.session.get(NATIVE_LAUNCH_KEY))[NATIVE_LAUNCH_KEY];
    if (launch === undefined) return null;
    if (!exact(launch, ['version', 'journey_id', 'draft_scope', 'tab_id', 'document_id', 'url',
      'created_at', 'expires_at', 'phase']) || launch.version !== 1 || launch.phase !== 'pending'
      || !UUID.test(launch.journey_id) || !validScope(launch.draft_scope)
      || !Number.isInteger(launch.tab_id) || launch.tab_id < 0 || typeof launch.document_id !== 'string'
      || !launch.document_id || launch.url !== reference('extension', launch.journey_id)
      || !Number.isFinite(launch.created_at) || launch.created_at > now()
      || launch.expires_at !== launch.created_at + NATIVE_LAUNCH_TTL_MS) fail('task_changed');
    return launch;
  }
  async function inspectLaunchDocument(launch) {
    // Intent is not a live task owner. Retire only an exactly identified or
    // confirmed replaced/closed document; an unavailable inspection fails closed.
    try {
      if (typeof chromeApi.runtime.getContexts !== 'function') fail('task_changed');
      const contexts = await extensionContexts();
      const current = contexts.filter(value => value.tabId === launch.tab_id && value.frameId === 0);
      if (current.length > 1) fail('task_changed');
      if (current.length === 1) {
        if (!current[0].documentId || !current[0].documentUrl) fail('task_changed');
        return;
      }
      const tabs = await chromeApi.tabs.query({});
      const tab = tabs.find(value => value.id === launch.tab_id);
      if (tab && (!tab.url || tab.url === launch.url)) fail('task_changed');
    } catch { fail('task_changed'); }
  }
  return Object.freeze({
    read: () => serialize(readRecord),
    // Save only presentation coordinates for an admitted desktop-first task.
    // The shared task owner fences other tasks and live authentication flows.
    adoptBrowserTask: (request, canAdopt = () => false, authorize = async () => {}) => serialize(async () => {
      if (!exact(request, ['journey_id', 'draft_scope']) || !UUID.test(request.journey_id)
        || !validScope(request.draft_scope)) fail('workspace_reference_invalid');
      const existing = await readRecord();
      const rebind = existing?.journey_id !== request.journey_id;
      const launch = await pendingLaunch();
      const tab = (await chromeApi.storage.session.get(WORKSPACE_TAB_KEY))[WORKSPACE_TAB_KEY];
      const retireLaunch = launch && (rebind || launch.journey_id !== existing.journey_id
        || launch.draft_scope.scope_id !== existing.draft_scope.scope_id
        || launch.draft_scope.disclosure_bundle_id !== existing.draft_scope.disclosure_bundle_id);
      const retireTab = tab && tab.journey_id !== request.journey_id;
      if (!rebind && !retireLaunch && !retireTab) return existing;
      if (canAdopt() !== true) fail('task_changed');
      if (retireLaunch) await inspectLaunchDocument(launch);
      await authorize();
      if (canAdopt() !== true) fail('task_changed');
      const record = rebind ? { version: 1, journey_id: request.journey_id, route: 'extension',
        draft_scope: { ...request.draft_scope, ...(existing ? { scope_id: crypto.randomUUID() } : {}) }, draft: blankDraft() }
        : existing;
      // The canonical record commits first. If cleanup fails, an exact retry
      // finishes only obsolete presentation cleanup without rotating its scope.
      if (rebind) await saveRecord(record);
      await authorize();
      if (canAdopt() !== true) fail('task_changed');
      const retire = [...(retireLaunch ? [NATIVE_LAUNCH_KEY] : []), ...(retireTab ? [WORKSPACE_TAB_KEY] : [])];
      if (retire.length) await chromeApi.storage.session.remove(retire);
      return record;
    }),
    admit: (sender) => serialize(async () => { const { record, parsed } = await admitted(sender); return { record, route: parsed.route }; }),
    prepareNativeLaunch: (sender) => serialize(async () => {
      const { record, tab, parsed } = await admitted(sender);
      if (parsed.route !== 'extension' || typeof tab.documentId !== 'string') fail('workspace_launch_unavailable');
      const createdAt = now();
      await chromeApi.storage.session.set({ [NATIVE_LAUNCH_KEY]: {
        version: 1, journey_id: record.journey_id, draft_scope: { ...record.draft_scope },
        tab_id: tab.id, document_id: tab.documentId, url: tab.url,
        created_at: createdAt, expires_at: createdAt + NATIVE_LAUNCH_TTL_MS, phase: 'pending',
      } });
      return { journey_id: record.journey_id, app_link: workspaceAppLink(record.journey_id) };
    }),
    refreshScope: (scope) => serialize(async () => {
      if (!validScope(scope)) fail('workspace_scope_invalid');
      const record = await readRecord();
      if (!record) return null;
      if (record.draft_scope.disclosure_bundle_id !== scope.disclosure_bundle_id) record.draft = blankDraft();
      else if (record.draft_scope.scope_id !== scope.scope_id) record.draft.full_checked = false;
      record.draft_scope = { ...scope };
      await saveRecord(record);
      return record;
    }),
    // Presentation only. Linking the marker to a fresh scope makes either
    // partial write fail closed; the marker never establishes account authority.
    reconcileIdentity: (request) => serialize(async () => {
      if (!exact(request, ['account_digest']) || (request.account_digest !== null
        && !/^[0-9a-f]{64}$/u.test(request.account_digest))) fail('workspace_identity_invalid');
      const record = await readRecord();
      if (!record) return null;
      const previous = (await localStorage.get(WORKSPACE_IDENTITY_KEY))[WORKSPACE_IDENTITY_KEY];
      if (exact(previous, ['version', 'journey_id', 'scope_id', 'disclosure_bundle_id', 'account_digest'])
        && previous.version === 1 && previous.journey_id === record.journey_id
        && previous.scope_id === record.draft_scope.scope_id
        && previous.disclosure_bundle_id === record.draft_scope.disclosure_bundle_id
        && previous.account_digest === request.account_digest) return record;
      record.draft.full_checked = false;
      record.draft_scope.scope_id = crypto.randomUUID();
      const identity = { version: 1, journey_id: record.journey_id, ...record.draft_scope,
        account_digest: request.account_digest };
      await localStorage.set({ [WORKSPACE_RECORD_KEY]: record,
        [WORKSPACE_ACTIVITY_KEY]: now(), [WORKSPACE_IDENTITY_KEY]: identity });
      return record;
    }),
    // A worker-only install continuation. Adopts a uniquely identified existing
    // journey without focusing it. The route change refreshes only its setup
    // document when Chrome cannot expose runtime to a pre-install document.
    resumeExisting: (request) => serialize(async () => {
      if (!exact(request, ['draft_scope', 'route']) || !validScope(request.draft_scope)
        || !ROUTES.includes(request.route)) fail('workspace_resume_invalid');
      let record = await readRecord();
      const candidates = await registeredTabs();
      const journeys = new Set(candidates.map((tab) => parse(tab.url).journeyId));
      if (!record && journeys.size > 1) fail('workspace_journey_conflict');
      const tab = record ? candidates.find((item) => parse(item.url).journeyId === record.journey_id) : candidates[0];
      if (!tab) return null;
      const { journeyId } = parse(tab.url);
      record ??= { version: 1, journey_id: journeyId, route: request.route,
        draft_scope: { ...request.draft_scope }, draft: blankDraft() };
      record.route = request.route;
      await saveRecord(record);
      await storeTab(tab, record.journey_id);
      await chromeApi.tabs.update(tab.id, { url: reference(request.route, record.journey_id) });
      return { journey_id: record.journey_id, tab_id: tab.id, route: record.route };
    }),
    // An explicit open may adopt a first-party page that predates extension
    // installation. Background state notifications never call this operation.
    open: (request) => serialize(async () => {
      if (!exact(request, ['journey_id', 'route', 'explicit', 'draft_scope']) || request.explicit !== true
        || !validScope(request.draft_scope)) fail('workspace_explicit_open_required');
      const url = reference(request.route, request.journey_id);
      let record = await readRecord();
      if (record && record.journey_id !== request.journey_id) fail('workspace_journey_conflict');
      const tab = await liveTab(request.journey_id);
      record ??= { version: 1, journey_id: request.journey_id, route: request.route,
        draft_scope: { ...request.draft_scope }, draft: blankDraft() };
      if (tab) record.route = parse(tab.url).route;
      await saveRecord(record);
      const selected = tab ?? await chromeApi.tabs.create({ url, active: true });
      await storeTab(selected, record.journey_id);
      if (tab) await chromeApi.tabs.update(tab.id, { active: true });
      await chromeApi.windows.update(selected.windowId, { focused: true });
      return { journey_id: record.journey_id, tab_id: selected.id, route: record.route };
    }),
    navigate: (sender, request) => serialize(async () => {
      if (!exact(request, ['route'])) fail('workspace_route_invalid');
      const { record, tab } = await admitted(sender);
      const url = reference(request.route, record.journey_id);
      await chromeApi.tabs.update(tab.id, { url });
      record.route = request.route;
      await saveRecord(record);
      return { journey_id: record.journey_id, tab_id: tab.id, route: record.route };
    }),
    saveDraft: (sender, request) => serialize(async () => {
      if (!exact(request, ['scope_id', 'draft'])) fail('workspace_draft_invalid');
      const { draft } = request;
      if (!exact(draft, DRAFT_FIELDS) || DRAFT_FIELDS.some((key) => typeof draft[key] !== 'boolean')) fail('workspace_draft_invalid');
      const { record } = await admitted(sender);
      if (request.scope_id !== record.draft_scope.scope_id) fail('workspace_draft_stale');
      record.draft = { ...draft };
      await saveRecord(record);
      return { ...record.draft };
    }),
    resetDraftScope: (sender, request) => serialize(async () => {
      if (!exact(request, ['expected_scope_id', 'draft_scope']) || !validScope(request.draft_scope)) fail('workspace_scope_invalid');
      const { record } = await admitted(sender);
      if (request.expected_scope_id !== record.draft_scope.scope_id
        || request.draft_scope.scope_id === record.draft_scope.scope_id) fail('workspace_draft_stale');
      // Instrument changes invalidate all draft choices; a creator change
      // invalidates Full analytics choice while preserving unchanged disclosures.
      if (request.draft_scope.disclosure_bundle_id !== record.draft_scope.disclosure_bundle_id) record.draft = blankDraft();
      else record.draft.full_checked = false;
      record.draft_scope = { ...request.draft_scope };
      await saveRecord(record);
      return { draft_scope: { ...record.draft_scope }, draft: { ...record.draft } };
    }),
  });
}
