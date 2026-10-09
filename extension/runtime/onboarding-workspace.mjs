// Navigation and unfinished checkbox choices only. This module cannot grant
// consent, authenticate a creator, pair a computer, or start capture.
import { NATIVE_LAUNCH_KEY, NATIVE_LAUNCH_TTL_MS, NATIVE_RECOVERY_KEY, NATIVE_RECOVERY_TTL_MS,
  NATIVE_RETURN_PATH, nativeReturnJourney, workspaceAppLink } from './onboarding-native-launch.mjs';
export const WORKSPACE_RECORD_KEY = 'onboarding_workspace_v1';
export const WORKSPACE_TAB_KEY = 'onboarding_workspace_tab_v1';
export const WORKSPACE_ACTIVITY_KEY = 'onboarding_workspace_activity_v1';
export const WORKSPACE_IDENTITY_KEY = 'onboarding_workspace_identity_v1';
const MAX_IDLE_MS = 30 * 24 * 60 * 60 * 1000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const ROUTES = ['extension', 'hosted', 'provisioning', 'bridge'];
const DRAFT_FIELDS = ['terms_checked', 'risk_checked', 'full_checked'];
const validScope = (scope) => exact(scope, ['scope_id', 'disclosure_bundle_id']) && UUID.test(scope.scope_id)
  && /^[0-9a-f]{64}$/u.test(scope.disclosure_bundle_id);
const blankDraft = () => Object.fromEntries(DRAFT_FIELDS.map((key) => [key, false]));
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, keys) => isObject(value) && Object.keys(value).length === keys.length
  && keys.every((key) => Object.hasOwn(value, key));
function fail(code) { throw new Error(code); }

/** Build-time routes, never a URL supplied by an incoming command. */
export function createOnboardingWorkspace({ chromeApi = globalThis.chrome, routes, now = Date.now, navigationTimeoutMs = 10_000 }) {
  if (!exact(routes, ROUTES)) fail('workspace_routes_invalid');
  const registered = new Map();
  for (const route of ROUTES) {
    if (route === 'hosted' && routes[route] === null) continue;
    const url = new URL(routes[route]);
    const extension = chromeApi.runtime.getURL('setup.html');
    if (url.username || url.password || url.search || url.hash || url.href !== routes[route]
      || (route === 'extension' ? url.href !== extension
        : !(url.protocol === 'https:' || (url.protocol === 'http:' && url.hostname === 'bridge.localhost')))) {
      fail('workspace_routes_invalid');
    }
    if (registered.has(url.href)) fail('workspace_routes_invalid');
    registered.set(url.href, route);
    if (route === 'hosted' && url.protocol === 'https:' && url.pathname === '/public/onboarding/setup') {
      registered.set(`${url.origin}/public/onboarding/installation-continuation`, 'hosted');
    }
  }
  let queue = Promise.resolve();
  let activityRevision = 0;
  chromeApi.tabs.onActivated?.addListener(() => { activityRevision++; });
  chromeApi.windows.onFocusChanged?.addListener(() => { activityRevision++; });
  const navigationPorts = new Map();
  const localOrigin = new URL(routes.bridge).origin;
  const nativeEntryUrl = `${localOrigin}${NATIVE_RETURN_PATH}`;
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
  const saveRecord = (record) => chromeApi.storage.local.set({ [WORKSPACE_RECORD_KEY]: record, [WORKSPACE_ACTIVITY_KEY]: now() });
  const readRecord = async () => {
    const touched = (await chromeApi.storage.local.get(WORKSPACE_ACTIVITY_KEY))[WORKSPACE_ACTIVITY_KEY];
    if (Number.isFinite(touched) && (now() - touched > MAX_IDLE_MS || touched > now())) {
      await chromeApi.storage.local.remove?.([WORKSPACE_RECORD_KEY, WORKSPACE_ACTIVITY_KEY, WORKSPACE_IDENTITY_KEY]);
      return null;
    }
    const record = (await chromeApi.storage.local.get(WORKSPACE_RECORD_KEY))[WORKSPACE_RECORD_KEY];
    if (!exact(record, ['version', 'journey_id', 'route', 'draft_scope', 'draft']) || record.version !== 1
      || !UUID.test(record.journey_id) || !ROUTES.includes(record.route)
      || !validScope(record.draft_scope)
      || !exact(record.draft, DRAFT_FIELDS) || DRAFT_FIELDS.some((key) => typeof record.draft[key] !== 'boolean')) return null;
    return record;
  };
  const storeTab = (tab, journeyId) => chromeApi.storage.session.set({
    [WORKSPACE_TAB_KEY]: { version: 1, journey_id: journeyId, tab_id: tab.id, window_id: tab.windowId },
  });
  const extensionContexts = async () => typeof chromeApi.runtime.getContexts === 'function'
    ? chromeApi.runtime.getContexts({ contextTypes: ['TAB'] }) : [];
  const getTab = async (id) => {
    const tab = await chromeApi.tabs.get(id);
    const context = (await extensionContexts()).find((value) => value.tabId === id && value.frameId === 0);
    // Always compare the current document, including reload to the same URL.
    if (context) return { ...tab, url: context.documentUrl, documentId: context.documentId };
    if (tab.url && (parse(tab.url) || tab.url === nativeEntryUrl || nativeReturnJourney(tab.url, localOrigin)) && chromeApi.scripting?.executeScript) {
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
  async function nativeCaller(sender, { bare = false, journeyId = null } = {}) {
    const validUrl = bare ? sender?.url === nativeEntryUrl
      : journeyId !== null ? sender?.url === nativeEntryUrl || nativeReturnJourney(sender?.url, localOrigin) === journeyId
        : sender?.url === nativeEntryUrl || nativeReturnJourney(sender?.url, localOrigin) !== null;
    if (!validUrl || sender?.id !== undefined || sender?.frameId !== 0
      || !Number.isInteger(sender?.tab?.id) || typeof sender.documentId !== 'string') fail('return_unavailable');
    const source = await getTab(sender.tab.id);
    if (source.url !== sender.url || source.documentId !== sender.documentId) fail('return_unavailable');
    return source;
  }
  async function nativeOwner() {
    const record = await readRecord();
    const owner = record ? await liveTab(record.journey_id) : null;
    if (!owner) {
      // An expired record is not evidence that its page has disappeared.
      if ((await registeredTabs()).length > 0) fail('workspace_exists');
      fail('no_workspace');
    }
    return { record, owner };
  }
  const currentLaunch = (intent, record, owner) => intent?.version === 1
    && Number.isSafeInteger(intent.created_at) && Number.isSafeInteger(intent.expires_at)
    && intent.expires_at === intent.created_at + NATIVE_LAUNCH_TTL_MS
    && intent.created_at <= now() && intent.expires_at > now()
    && intent.journey_id === record.journey_id && intent.tab_id === owner.id
    && intent.draft_scope?.scope_id === record.draft_scope.scope_id
    && intent.draft_scope?.disclosure_bundle_id === record.draft_scope.disclosure_bundle_id;
  function watchNavigation(owner, target) {
    let settle, done = false;
    const promise = new Promise((resolve) => { settle = resolve; });
    const finish = (value) => {
      if (done) return; done = true;
      clearTimeout(timer); chromeApi.tabs.onUpdated.removeListener(changed); settle(value);
    };
    const changed = (id, change) => {
      if (id !== owner.id || change.status !== 'complete') return;
      void getTab(id).then((current) => {
        if (current.url === target && current.documentId && current.documentId !== owner.documentId) finish(current);
        else if (current.url !== owner.url && current.url !== target) finish(null);
      }, () => finish(null));
    };
    const timer = setTimeout(() => finish(null), navigationTimeoutMs);
    chromeApi.tabs.onUpdated.addListener(changed);
    return { promise, cancel: () => finish(null) };
  }
  const sameDocument = (tab, expected) => tab?.id === expected?.tab_id
    && tab.documentId === expected.document_id && tab.url === expected.url;
  const documentReference = (tab) => ({ tab_id: tab.id, document_id: tab.documentId, url: tab.url });
  const sameScope = (a, b) => a?.scope_id === b?.scope_id && a?.disclosure_bundle_id === b?.disclosure_bundle_id;
  async function identityFor(record) {
    const identity = (await chromeApi.storage.local.get(WORKSPACE_IDENTITY_KEY))[WORKSPACE_IDENTITY_KEY];
    if (!exact(identity, ['version', 'journey_id', 'scope_id', 'disclosure_bundle_id', 'account_digest'])
      || identity.version !== 1 || identity.journey_id !== record.journey_id || !sameScope(identity, record.draft_scope)
      || (identity.account_digest !== null && !/^[0-9a-f]{64}$/u.test(identity.account_digest))) fail('return_unavailable');
    return identity;
  }
  async function recoveryOwner(record, { savedContinuation = false } = {}) {
    const candidates = await registeredTabs();
    if (candidates.length > 1 || candidates.some((tab) => parse(tab.url).journeyId !== record.journey_id)) fail('workspace_exists');
    const owner = candidates[0] ? await getTab(candidates[0].id) : null;
    const eligible = savedContinuation ? ['extension', 'provisioning', 'hosted'] : ['extension', 'provisioning'];
    if (owner && (!owner.documentId || !eligible.includes(parse(owner.url)?.route))) fail('workspace_exists');
    if (!owner && !eligible.includes(record.route)) fail('workspace_exists');
    return owner;
  }
  async function focusWhileCurrent(source, owner, currentScope = () => true) {
    const activity = activityRevision;
    const current = await getTab(source.id);
    const window = await chromeApi.windows.get(source.windowId);
    if (!sameDocument(current, documentReference(source)) || current.active !== true || window.focused !== true) return;
    const currentOwner = await getTab(owner.id);
    if (!sameDocument(currentOwner, documentReference(owner)) || !currentScope()) fail('return_unavailable');
    if (activity !== activityRevision) return;
    await chromeApi.tabs.update(owner.id, { active: true });
    if (owner.windowId !== source.windowId && activity === activityRevision && currentScope()) {
      await chromeApi.windows.update(owner.windowId, { focused: true });
    }
  }
  async function recoveryIntent() {
    const intent = (await chromeApi.storage.session.get(NATIVE_RECOVERY_KEY))[NATIVE_RECOVERY_KEY];
    const document = (value) => exact(value, ['tab_id', 'document_id', 'url']) && Number.isInteger(value.tab_id)
      && typeof value.document_id === 'string' && typeof value.url === 'string';
    const keys = ['version', 'recovery_id', 'entry_id', 'previous_journey_id', 'draft_scope', 'account_digest',
      'source', 'owner', 'created_at', 'expires_at', 'phase', 'journey_id', 'target_document_id'];
    const saved = intent?.version === 2 && intent.saved_continuation === true;
    if (saved) keys.push('saved_continuation');
    if (!exact(intent, keys) || (!saved && intent.version !== 1)
      || !UUID.test(intent.recovery_id) || !UUID.test(intent.entry_id) || !UUID.test(intent.previous_journey_id)
      || !validScope(intent.draft_scope) || (intent.account_digest !== null && !/^[0-9a-f]{64}$/u.test(intent.account_digest))
      || !document(intent.source) || (intent.owner !== null && !document(intent.owner))
      || !Number.isSafeInteger(intent.created_at) || intent.created_at > now()
      || intent.expires_at !== intent.created_at + NATIVE_RECOVERY_TTL_MS || intent.expires_at <= now()
      || !['prepared', 'returning', 'returned'].includes(intent.phase)
      || (intent.journey_id !== null && !UUID.test(intent.journey_id))
      || (intent.target_document_id !== null && typeof intent.target_document_id !== 'string')) return null;
    return intent;
  }
  async function finishRecovery(intent, current, record, identity) {
    // This changes presentation ownership only. The destination authenticates
    // the selected journey using its own local session.
    record.journey_id = intent.journey_id; record.route = 'provisioning';
    identity.journey_id = intent.journey_id;
    await chromeApi.storage.local.set({ [WORKSPACE_RECORD_KEY]: record, [WORKSPACE_IDENTITY_KEY]: identity,
      [WORKSPACE_ACTIVITY_KEY]: now() });
    await storeTab(current, record.journey_id);
    intent.phase = 'returned'; intent.target_document_id = current.documentId;
    await chromeApi.storage.session.set({ [NATIVE_RECOVERY_KEY]: intent });
    return { status: intent.owner === null ? 'continued' : 'returned' };
  }
  return Object.freeze({
    reference,
    read: () => serialize(readRecord),
    bindNavigationPort: async (port) => {
      let closed = false, key = null;
      port.onDisconnect.addListener(() => {
        closed = true; if (key && navigationPorts.get(key) === port) navigationPorts.delete(key);
      });
      try {
        const { tab, parsed } = await admitted(port.sender);
        if (closed || parsed.route !== 'extension' || !tab.documentId) throw Error('workspace_sender_invalid');
        key = `${tab.id}:${tab.documentId}`;
        navigationPorts.set(key, port);
        port.postMessage({ type: 'ready' });
      } catch { port.disconnect(); }
    },
    admit: (sender) => serialize(async () => { const { record, parsed } = await admitted(sender); return { record, route: parsed.route }; }),
    focus: (sender) => serialize(async () => {
      const { tab, parsed } = await admitted(sender);
      if (parsed.route === 'hosted') fail('workspace_local_only');
      await chromeApi.tabs.update(tab.id, { active: true });
      await chromeApi.windows.update(tab.windowId, { focused: true });
    }),
    prepareNativeLaunch: (sender) => serialize(async () => {
      const { record, tab, parsed } = await admitted(sender);
      if (parsed.route !== 'extension' || typeof tab.documentId !== 'string') fail('workspace_launch_unavailable');
      const previous = (await chromeApi.storage.session.get(NATIVE_LAUNCH_KEY))[NATIVE_LAUNCH_KEY];
      // Repeated explicit clicks can reuse a still-valid intent, but never
      // replace an uncertain return or authorize a different document.
      if (previous?.phase === 'returning' && previous.expires_at > now()) fail('workspace_return_unconfirmed');
      const createdAt = now();
      await chromeApi.storage.session.set({ [NATIVE_LAUNCH_KEY]: {
        version: 1, journey_id: record.journey_id, draft_scope: { ...record.draft_scope },
        tab_id: tab.id, document_id: tab.documentId, url: tab.url,
        created_at: createdAt, expires_at: createdAt + NATIVE_LAUNCH_TTL_MS, phase: 'pending',
      } });
      return { journey_id: record.journey_id, app_link: workspaceAppLink(record.journey_id) };
    }),
    discoverNativeLaunch: (sender) => serialize(async () => {
      const source = await nativeCaller(sender, { bare: true });
      const { record, owner } = await nativeOwner();
      const intent = (await chromeApi.storage.session.get(NATIVE_LAUNCH_KEY))[NATIVE_LAUNCH_KEY];
      if (!currentLaunch(intent, record, owner) || intent.phase !== 'pending' || source.id === owner.id
        || owner.url !== intent.url || owner.documentId !== intent.document_id) fail('workspace_exists');
      return { status: 'pending_launch', journey_id: record.journey_id };
    }),
    focusFromNative: (sender) => serialize(async () => {
      await nativeCaller(sender);
      const { owner } = await nativeOwner();
      await chromeApi.tabs.update(owner.id, { active: true });
      await chromeApi.windows.update(owner.windowId, { focused: true });
      return { status: 'focused' };
    }),
    prepareNativeRecovery: (sender, request, currentScope = () => true, { savedContinuation = false } = {}) => serialize(async () => {
      if (!exact(request, ['entry_id']) || !UUID.test(request.entry_id) || !currentScope()) fail('return_unavailable');
      const source = await nativeCaller(sender);
      const record = await readRecord();
      if (!record) {
        if ((await registeredTabs()).length) fail('workspace_exists');
        fail('no_workspace');
      }
      const targeted = nativeReturnJourney(source.url, localOrigin);
      if (targeted !== null && targeted !== record.journey_id) fail('workspace_exists');
      const identity = await identityFor(record);
      const owner = await recoveryOwner(record, { savedContinuation });
      if (owner?.id === source.id) fail('return_unavailable');
      const launch = (await chromeApi.storage.session.get(NATIVE_LAUNCH_KEY))[NATIVE_LAUNCH_KEY];
      if (!currentScope()) fail('return_unavailable');
      if (!savedContinuation && owner && currentLaunch(launch, record, owner) && launch.phase === 'pending'
        && sameDocument(owner, { tab_id: launch.tab_id, document_id: launch.document_id, url: launch.url })) {
        return { status: 'launch_pending', journey_id: record.journey_id };
      }
      if (!savedContinuation && owner && parse(owner.url)?.route === 'extension' && launch?.phase === 'pending'
        && launch.version === 1 && launch.journey_id === record.journey_id && sameScope(launch.draft_scope, record.draft_scope)
        && Number.isSafeInteger(launch.created_at) && launch.created_at <= now()
        && launch.expires_at === launch.created_at + NATIVE_LAUNCH_TTL_MS && launch.expires_at <= now()
        && sameDocument(owner, { tab_id: launch.tab_id, document_id: launch.document_id, url: launch.url })) {
        await focusWhileCurrent(source, owner, currentScope);
        if (!currentScope()) fail('return_unavailable');
        return { status: 'launch_expired' };
      }
      const previous = await recoveryIntent();
      if (!currentScope()) fail('return_unavailable');
      if (previous?.phase === 'returning') fail('return_unavailable');
      if (previous?.phase === 'prepared' && previous.entry_id === request.entry_id && sameDocument(source, previous.source)
        && (previous.saved_continuation === true) === savedContinuation
        && previous.previous_journey_id === record.journey_id && sameScope(previous.draft_scope, record.draft_scope)
        && previous.account_digest === identity.account_digest
        && (previous.owner === null ? owner === null : sameDocument(owner, previous.owner))) {
        return { status: 'recovery_ready', recovery_id: previous.recovery_id, previous_journey_id: record.journey_id };
      }
      const createdAt = now();
      const intent = { version: savedContinuation ? 2 : 1, ...(savedContinuation ? { saved_continuation: true } : {}),
        recovery_id: crypto.randomUUID(), entry_id: request.entry_id,
        previous_journey_id: record.journey_id, draft_scope: { ...record.draft_scope }, account_digest: identity.account_digest,
        source: documentReference(source), owner: owner ? documentReference(owner) : null,
        created_at: createdAt, expires_at: createdAt + NATIVE_RECOVERY_TTL_MS, phase: 'prepared',
        journey_id: null, target_document_id: null };
      await chromeApi.storage.session.set({ [NATIVE_RECOVERY_KEY]: intent });
      return { status: 'recovery_ready', recovery_id: intent.recovery_id, previous_journey_id: record.journey_id };
    }),
    reattachNativeRecovery: (sender, request, currentScope = () => true) => serialize(async () => {
      if (!exact(request, ['entry_id', 'recovery_id', 'previous_journey_id', 'journey_id'])
        || !Object.values(request).every((value) => typeof value === 'string' && UUID.test(value))
        || !currentScope()) fail('return_unavailable');
      const source = await nativeCaller(sender);
      const intent = await recoveryIntent();
      if (!intent || intent.saved_continuation !== true || source.id !== intent.source.tab_id || source.url !== intent.source.url
        || request.entry_id !== intent.entry_id || request.recovery_id !== intent.recovery_id
        || request.previous_journey_id !== intent.previous_journey_id
        || (intent.journey_id !== null && intent.journey_id !== request.journey_id)) fail('return_unavailable');
      const original = JSON.stringify(intent);
      const check = async () => {
        const record = await readRecord();
        if (!record || ![intent.previous_journey_id, intent.journey_id].includes(record.journey_id)
          || !sameScope(record.draft_scope, intent.draft_scope)) fail('return_unavailable');
        const identity = await identityFor(record);
        if (identity.account_digest !== intent.account_digest) fail('return_unavailable');
        if (intent.phase === 'prepared') {
          const owner = await recoveryOwner(record, { savedContinuation: intent.saved_continuation === true });
          if (intent.owner === null ? owner !== null : !sameDocument(owner, intent.owner)) fail('workspace_exists');
        } else {
          // After dispatch this operation may only recover the committed target;
          // it never resets the phase or sends another navigation command.
          if (intent.journey_id === null) fail('return_unavailable');
          const target = await getTab(intent.owner?.tab_id ?? source.id);
          if (target.url !== reference('provisioning', intent.journey_id) || !target.documentId
            || target.documentId === (intent.owner ?? intent.source).document_id
            || (intent.target_document_id !== null && intent.target_document_id !== target.documentId)) fail('return_unavailable');
          const candidates = await registeredTabs();
          if (candidates.length !== 1 || candidates[0].id !== target.id) fail('workspace_exists');
        }
        if (JSON.stringify(await recoveryIntent()) !== original || !currentScope()) fail('return_unavailable');
        await nativeCaller(sender);
        return { record, identity };
      };
      const before = await check();
      const after = await check();
      if (JSON.stringify(before) !== JSON.stringify(after) || !currentScope()) fail('return_unavailable');
      await nativeCaller(sender);
      if (!currentScope() || intent.created_at > now() || intent.expires_at <= now()) fail('return_unavailable');
      // The callback has independently read its local selection receipt. These
      // coordinates restore navigation ownership only; no local proof is shared.
      intent.source = documentReference(source);
      if (intent.phase === 'prepared') intent.journey_id = request.journey_id;
      await chromeApi.storage.session.set({ [NATIVE_RECOVERY_KEY]: intent });
      await nativeCaller(sender);
      if (!currentScope() || intent.created_at > now() || intent.expires_at <= now()) fail('return_unavailable');
      return { status: 'reattached' };
    }),
    returnFromNativeRecovery: (sender, request, currentScope = () => true) => serialize(async () => {
      if (!exact(request, ['entry_id', 'recovery_id', 'previous_journey_id', 'journey_id', 'route'])
        || !['entry_id', 'recovery_id', 'previous_journey_id', 'journey_id'].every((key) => UUID.test(request[key]))
        || request.route !== 'provisioning' || !currentScope()) fail('return_unavailable');
      const source = await nativeCaller(sender);
      const intent = await recoveryIntent();
      if (!intent || !sameDocument(source, intent.source) || request.entry_id !== intent.entry_id
        || request.recovery_id !== intent.recovery_id || request.previous_journey_id !== intent.previous_journey_id
        || (intent.journey_id !== null && intent.journey_id !== request.journey_id)) fail('return_unavailable');
      const record = await readRecord();
      if (!record || ![intent.previous_journey_id, intent.journey_id].includes(record.journey_id)
        || !sameScope(record.draft_scope, intent.draft_scope)) fail('return_unavailable');
      const identity = await identityFor(record);
      if (identity.account_digest !== intent.account_digest) fail('return_unavailable');
      const target = reference('provisioning', request.journey_id);
      if (intent.phase !== 'prepared') {
        const current = await getTab(intent.owner?.tab_id ?? source.id);
        if (current.url !== target || !current.documentId || current.documentId === (intent.owner ?? intent.source).document_id
          || (intent.target_document_id !== null && current.documentId !== intent.target_document_id)) fail('return_unavailable');
        const candidates = await registeredTabs();
        if (candidates.length !== 1 || candidates[0].id !== current.id || !currentScope()) fail('workspace_exists');
        return finishRecovery(intent, current, record, identity);
      }
      const owner = await recoveryOwner(record, { savedContinuation: intent.saved_continuation === true });
      if (intent.owner === null ? owner !== null : !sameDocument(owner, intent.owner)) fail('workspace_exists');
      const destination = owner ?? source;
      intent.phase = 'returning'; intent.journey_id = request.journey_id;
      await chromeApi.storage.session.set({ [NATIVE_RECOVERY_KEY]: intent });
      await nativeCaller(sender);
      if (owner && !sameDocument(await getTab(owner.id), intent.owner)) fail('return_unavailable');
      if (!currentScope()) fail('return_unavailable');
      const navigation = watchNavigation(destination, target);
      try {
        if (owner && parse(owner.url)?.route === 'extension') {
          const port = navigationPorts.get(`${owner.id}:${owner.documentId}`);
          if (!port) throw Error('return_unavailable');
          port.postMessage({ type: 'recover', request_id: crypto.randomUUID(), previous_journey_id: intent.previous_journey_id,
            journey_id: request.journey_id, draft_scope: { ...intent.draft_scope }, expected_url: owner.url, route: 'provisioning' });
        } else {
          // Exact document targeting and the synchronous URL guard prevent a
          // reused tab or a different document from receiving navigation.
          await chromeApi.scripting.executeScript({ target: { tabId: destination.id, documentIds: [destination.documentId] },
            world: 'ISOLATED', args: [destination.url, target], func: (expected, url) => {
              if (location.href !== expected || !/^http:\/\/bridge\.localhost:17871\/provisioning#journey=[0-9a-f-]{36}$/u.test(url)) return;
              history.replaceState(null, '', url); location.reload();
            } });
        }
      } catch { /* A lost acknowledgement is observed, never dispatched again. */ }
      const current = await navigation.promise;
      if (!current || !currentScope()) fail('return_unavailable');
      if (owner) {
        await nativeCaller(sender);
        await focusWhileCurrent(source, current, currentScope);
      }
      if (!currentScope()) fail('return_unavailable');
      return finishRecovery(intent, current, record, identity);
    }),
    returnFromNative: (sender, request) => serialize(async () => {
      if (!exact(request, ['journey_id', 'route']) || !UUID.test(request.journey_id)
        || !['provisioning', 'bridge'].includes(request.route)) fail('return_unavailable');
      const source = await nativeCaller(sender, { journeyId: request.journey_id });
      const { record, owner } = await nativeOwner();
      const intent = (await chromeApi.storage.session.get(NATIVE_LAUNCH_KEY))[NATIVE_LAUNCH_KEY];
      if (!currentLaunch(intent, record, owner) || record.journey_id !== request.journey_id
        || source.id === owner.id) fail('workspace_exists');
      const target = reference(request.route, record.journey_id);
      const sameReturn = intent.return_tab_id === source.id && intent.return_document_id === source.documentId
        && intent.return_route === request.route;
      if (intent.phase !== 'pending') {
        if (!sameReturn || !['returning', 'returned'].includes(intent.phase)) fail('workspace_exists');
        // A lost response is a read-only reconciliation. Do not repeat the
        // navigation, even if its outcome remains unknown.
        if (intent.phase === 'returning' && owner.url !== target) fail('return_unavailable');
        if (intent.phase === 'returned' && parse(owner.url)?.journeyId !== record.journey_id) fail('return_unavailable');
        record.route = parse(owner.url).route;
        await saveRecord(record); await storeTab(owner, record.journey_id);
        intent.phase = 'returned';
        await chromeApi.storage.session.set({ [NATIVE_LAUNCH_KEY]: intent });
        return { status: 'returned' };
      }
      if (owner.url !== intent.url || owner.documentId !== intent.document_id) fail('workspace_exists');
      intent.phase = 'returning'; intent.return_tab_id = source.id;
      intent.return_document_id = source.documentId; intent.return_route = request.route;
      await chromeApi.storage.session.set({ [NATIVE_LAUNCH_KEY]: intent });
      const returningTab = await getTab(source.id);
      if (returningTab.documentId !== source.documentId || returningTab.url !== source.url) fail('return_unavailable');
      const returningWindow = await chromeApi.windows.get(source.windowId);
      const foreground = returningTab.active === true && returningWindow.focused === true;
      let latest;
      try { latest = await nativeOwner(); } catch { fail('workspace_exists'); }
      if (!currentLaunch(intent, latest.record, latest.owner) || latest.owner.url !== intent.url
        || latest.owner.documentId !== intent.document_id) fail('workspace_exists');
      const port = navigationPorts.get(`${owner.id}:${intent.document_id}`);
      if (!port || !chromeApi.tabs.onUpdated) fail('return_unavailable');
      const navigation = watchNavigation(latest.owner, target);
      try {
        port.postMessage({ type: 'navigate', request_id: crypto.randomUUID(), journey_id: record.journey_id,
          draft_scope: { ...record.draft_scope }, expected_url: intent.url, route: request.route });
      } catch { navigation.cancel(); fail('return_unavailable'); }
      const current = await navigation.promise;
      if (!current) fail('return_unavailable');
      // A slow launch must not steal focus after the user switches elsewhere.
      // Returning from the still-foreground native page may select its owner.
      if (foreground) await focusWhileCurrent(source, current);
      record.route = request.route;
      await saveRecord(record); await storeTab(current, record.journey_id);
      intent.phase = 'returned';
      await chromeApi.storage.session.set({ [NATIVE_LAUNCH_KEY]: intent });
      return { status: 'returned' };
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
      const previous = (await chromeApi.storage.local.get(WORKSPACE_IDENTITY_KEY))[WORKSPACE_IDENTITY_KEY];
      if (exact(previous, ['version', 'journey_id', 'scope_id', 'disclosure_bundle_id', 'account_digest'])
        && previous.version === 1 && previous.journey_id === record.journey_id
        && previous.scope_id === record.draft_scope.scope_id
        && previous.disclosure_bundle_id === record.draft_scope.disclosure_bundle_id
        && previous.account_digest === request.account_digest) return record;
      record.draft.full_checked = false;
      record.draft_scope.scope_id = crypto.randomUUID();
      const identity = { version: 1, journey_id: record.journey_id, ...record.draft_scope,
        account_digest: request.account_digest };
      await chromeApi.storage.local.set({ [WORKSPACE_RECORD_KEY]: record,
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
