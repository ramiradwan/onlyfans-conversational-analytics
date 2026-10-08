import { DocumentObserverCoordinator, OBSERVER_STATE_TYPE, OBSERVER_REOPEN_TYPE } from './document-observer.mjs';
import { allowsUiMessage } from './ui-surfaces.mjs';
import { OperationScope, SerialExecutor } from './operation-scope.mjs';
import { DELETE_INTENT_KEY, deletionIntent } from './deletion-state.mjs';
import { LOCAL_SERVICE_PATTERN, LOCAL_SERVICE_HEALTH } from '../transport/local-service-endpoints.mjs';
import {
  PAGE_CONTROL_MESSAGE_TYPE,
  PAGE_CONTROL_VERSION,
  CAPTURE_STATE_QUERY_TYPE,
  PREVIEW_MESSAGE_TYPE,
  isPreviewEnvelope,
} from '../capture/envelopes.mjs';

export const CONSENT_STORAGE_KEY = 'ofca_consent_v1';
export const ACTIVE_ACCOUNT_PARTITION_KEY = 'active_account_partition_v5';
export const CONSENT_POLICY_REVISION = '1';
export const UI_STATUS_MESSAGE_TYPE = 'ofca.ui.status';
export const UI_TRANSITION_MESSAGE_TYPE = 'ofca.ui.transition';
export const UI_CLEAR_PREVIEW_MESSAGE_TYPE = 'ofca.ui.clear-preview';
export const UI_DELETE_LOCAL_DATA_MESSAGE_TYPE = 'ofca.ui.delete-local-data';
export const UI_RELOAD_TABS_MESSAGE_TYPE = 'ofca.ui.reload-tabs';
export const ONLYFANS_ORIGIN_PATTERN = 'https://onlyfans.com/*';
export const LOCAL_ANALYTICS_ORIGIN_PATTERN = LOCAL_SERVICE_PATTERN;
export const LOCAL_ANALYTICS_HEALTH_URL = LOCAL_SERVICE_HEALTH;
export const PREVIEW_PRUNE_ALARM_NAME = 'ofca-preview-retention';

const SCRIPT_MODES = Object.freeze(['identity', 'preview', 'full']);
const ACTIVE_CONSENT_MODES = new Set(['preview', 'full']);
const ALL_CONSENT_MODES = new Set(['off', 'preview', 'full', 'paused', 'revoked']);

export function defaultConsentState() {
  return {
    schema: 'ofca-consent/v2',
    mode: 'off',
    resume_mode: null,
    policy_revision: CONSENT_POLICY_REVISION,
    updated_at: null,
    authorization_event_id: null,
    consent_epoch: crypto.randomUUID(),
  };
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function validatedState(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || !ALL_CONSENT_MODES.has(value.mode)
    || (value.resume_mode !== null && !ACTIVE_CONSENT_MODES.has(value.resume_mode))
    || (value.updated_at !== null && !Number.isFinite(Date.parse(value.updated_at)))
    || typeof value.policy_revision !== 'string') return defaultConsentState();
  if (value.schema === 'ofca-consent/v1' && Object.keys(value).length === 5) {
    return {
      ...defaultConsentState(),
      mode: ACTIVE_CONSENT_MODES.has(value.mode) ? 'paused' : value.mode,
      resume_mode: ACTIVE_CONSENT_MODES.has(value.mode) ? value.mode : value.resume_mode,
      updated_at: value.updated_at,
    };
  }
  if (value.schema !== 'ofca-consent/v2' || Object.keys(value).length !== 7
    || !UUID.test(value.consent_epoch)
    || (value.authorization_event_id !== null && !UUID.test(value.authorization_event_id))) {
    return defaultConsentState();
  }
  const state = structuredClone(value);
  if (state.mode === 'revoked' || state.mode === 'off') state.authorization_event_id = null;
  if (state.policy_revision !== CONSENT_POLICY_REVISION && ACTIVE_CONSENT_MODES.has(state.mode)) {
    state.resume_mode = state.mode;
    state.mode = 'paused';
    state.consent_epoch = crypto.randomUUID();
  }
  return state;
}

function contentScriptsFor(mode) {
  if (!SCRIPT_MODES.includes(mode)) return [];
  return [
    {
      id: `ofca-${mode}-main`,
      matches: [ONLYFANS_ORIGIN_PATTERN],
      js: [`page-hook-mode-${mode}.js`, 'page-hook.js'],
      runAt: 'document_start',
      allFrames: false,
      persistAcrossSessions: true,
      world: 'MAIN',
    },
    {
      id: `ofca-${mode}-isolated`,
      matches: [ONLYFANS_ORIGIN_PATTERN],
      js: ['content.js'],
      runAt: 'document_start',
      allFrames: false,
      persistAcrossSessions: true,
      world: 'ISOLATED',
    },
  ];
}

function sameScripts(left, right) {
  const signatures = (scripts) => scripts.map((script) => JSON.stringify({
    id: script.id,
    matches: [...(script.matches ?? [])].sort(),
    excludeMatches: [...(script.excludeMatches ?? [])].sort(),
    js: script.js ?? [],
    css: script.css ?? [],
    runAt: script.runAt ?? 'document_idle',
    world: script.world ?? 'ISOLATED',
    allFrames: script.allFrames === true,
    matchOriginAsFallback: script.matchOriginAsFallback === true,
    persistAcrossSessions: script.persistAcrossSessions !== false,
  })).sort();
  return JSON.stringify(signatures(left)) === JSON.stringify(signatures(right));
}

function trustedContentSender(sender, chromeApi) {
  if (sender?.id !== chromeApi.runtime.id || sender?.frameId !== 0) return false;
  try {
    return new URL(sender.url).origin === 'https://onlyfans.com';
  } catch (_error) {
    return false;
  }
}



export class ConsentController {
  constructor({
    chromeApi = globalThis.chrome,
    runtime,
    adapter,
    provisioningIdentityBridge,
    previewMetrics,
    clearLocalData,
    activeModeAuthorization,
    captureScope = new OperationScope(),
    legalScope = new OperationScope(),
    controlQueue = new SerialExecutor(),
    activationEvidenceStore = null,
    runtimeSummary = () => ({}),
    hasSavedPairing = async () => false,
    scheduler = globalThis,
    fetchImpl = globalThis.fetch,
    now = () => new Date(),
  }) {
    if (!chromeApi?.storage?.local || !chromeApi?.scripting || !chromeApi?.permissions) {
      throw new Error('Consent controller requires Chrome storage, scripting, and permissions');
    }
    if (!runtime?.start && !runtime?.wake) throw new Error('Consent controller requires Agent runtime');
    if (
      typeof adapter?.loadBrainBinding !== 'function'
      || typeof adapter?.clearBrainBinding !== 'function'
    ) {
      throw new Error('Consent controller requires the Agent storage adapter');
    }
    if (
      !previewMetrics?.record
      || !previewMetrics?.summary
      || !previewMetrics?.clear
      || !previewMetrics?.prune
    ) {
      throw new Error('Consent controller requires preview metrics');
    }
    if (typeof clearLocalData !== 'function') {
      throw new Error('Consent controller requires a local data cleaner');
    }
    if (
      typeof activeModeAuthorization?.authorizeTransition !== 'function'
      || typeof activeModeAuthorization?.authorizeResume !== 'function'
      || typeof activeModeAuthorization?.reconcileActiveMode !== 'function'
    ) {
      throw new Error('Consent controller requires explicit active-mode authorization');
    }
    this.chromeApi = chromeApi;
    this.runtime = runtime;
    this.adapter = adapter;
    this.provisioningIdentityBridge = provisioningIdentityBridge;
    this.previewMetrics = previewMetrics;
    this.clearLocalData = clearLocalData;
    this.activeModeAuthorization = activeModeAuthorization;
    this.runtimeSummary = runtimeSummary;
    this.hasSavedPairing = hasSavedPairing;
    this.scheduler = scheduler;
    this.bindingRetryTimer = null;
    this.bindingRetryDelay = 500;
    this.bindingRetryAttempt = 0;
    this.fetchImpl = fetchImpl;
    this.now = now;
    this.captureScope = captureScope;
    this.legalScope = legalScope;
    this.controlQueue = controlQueue;
    this.activationEvidenceStore = activationEvidenceStore;
    this.controlGeneration = 0;
    this.controlAbort = new AbortController();
    this.loaded = false;
    this.deletionPending = false;
    this.documentReset = false;
    this.scriptMode = null;
    this.identityRefreshPending = false;
    this.state = defaultConsentState();
    this.phase = 'booting';
    this.initialization = null;
    this.registered = false;
    this.captureNotificationLastReportAt = null;
    this.captureNotificationTimer = null;
    this.captureNotificationPending = false;
    this.changeListeners = new Set();
    this.observer = new DocumentObserverCoordinator({ chromeApi, scheduler, changed: () => this.changed() });
    this.messageListener = this.#onMessage.bind(this);
    this.storageListener = this.#onStorageChanged.bind(this);
    this.permissionListener = () => { void this.reconcile().catch(() => undefined); };
    this.alarmListener = (alarm) => {
      if (alarm?.name === PREVIEW_PRUNE_ALARM_NAME) {
        void this.runLegalOperation(async ({ assertCurrent }) => {
          assertCurrent();
          await this.previewMetrics.prune();
        }).catch(() => undefined);
      }
    };
  }

  register() {
    if (this.registered) return;
    this.provisioningIdentityBridge.register();
    this.observer.start();
    this.chromeApi.runtime.onMessage.addListener(this.messageListener);
    this.chromeApi.storage.onChanged?.addListener(this.storageListener);
    this.chromeApi.permissions.onRemoved?.addListener(this.permissionListener);
    this.chromeApi.permissions.onAdded?.addListener(this.permissionListener);
    for (const event of ['onCreated', 'onRemoved', 'onUpdated', 'onReplaced']) {
      this.chromeApi.tabs?.[event]?.addListener(() => this.#requestCaptureStateNotificationReport());
    }
    this.chromeApi.alarms?.onAlarm?.addListener(this.alarmListener);
    const alarm = this.chromeApi.alarms?.create?.(PREVIEW_PRUNE_ALARM_NAME, {
      delayInMinutes: 1,
      periodInMinutes: 24 * 60,
    });
    alarm?.catch?.(() => undefined);
    this.registered = true;
  }

  #assertGeneration(generation) {
    if (generation !== this.controlGeneration) {
      throw Object.assign(new Error('stale_control'), { code: 'stale_control' });
    }
  }

  #invalidate(code) {
    this.controlGeneration += 1;
    if (this.bindingRetryTimer !== null) this.scheduler.clearTimeout(this.bindingRetryTimer);
    this.bindingRetryTimer = null;
    this.bindingRetryDelay = 500;
    this.bindingRetryAttempt = 0;
    this.adapter.invalidate?.();
    this.controlAbort.abort(Object.assign(new Error(code), { code }));
    this.controlAbort = new AbortController();
    this.captureScope.close(code);
    this.observer.invalidate();
    // Stop published senders and cancel startup at the call boundary, before queue admission.
    const stopping = this.#suspendRuntime();
    void stopping.catch(() => undefined);
    return this.controlGeneration;
  }

  async #loadStateLocked() {
    await this.chromeApi.storage.local.setAccessLevel?.({ accessLevel: 'TRUSTED_CONTEXTS' });
    if (!this.loaded) {
      const saved = await this.chromeApi.storage.local.get([CONSENT_STORAGE_KEY, DELETE_INTENT_KEY]);
      this.state = validatedState(saved?.[CONSENT_STORAGE_KEY]);
      this.deletionPending = Object.hasOwn(saved ?? {}, DELETE_INTENT_KEY);
      this.loaded = true;
      if (saved?.[CONSENT_STORAGE_KEY] && JSON.stringify(saved[CONSENT_STORAGE_KEY]) !== JSON.stringify(this.state)) {
        await this.chromeApi.storage.local.set({ [CONSENT_STORAGE_KEY]: this.state });
      }
    }
    if (this.deletionPending) await this.#deleteLocalDataLocked(false);
  }

  initialize() {
    if (this.initialization !== null) return this.initialization;
    this.register();
    const generation = this.controlGeneration;
    const attempt = this.controlQueue.run(async () => {
      await this.#loadStateLocked();
      this.#assertGeneration(generation);
      await this.previewMetrics.prune();
      await this.#reconcileLocked(generation);
      if (!this.legalScope.isOpen) this.legalScope.reopen();
      return this;
    });
    this.initialization = attempt;
    void attempt.catch(() => {
      if (this.initialization === attempt) this.initialization = null;
    });
    return attempt;
  }

  runLegalOperation(work) {
    const generation = this.controlGeneration;
    if (this.deletionPending) return Promise.reject(Object.assign(new Error('delete_incomplete'), { code: 'delete_incomplete' }));
    return this.initialize().then(() => this.controlQueue.run(() => this.legalScope.run(async (lease) => {
      const assertCurrent = () => { lease.assertCurrent(); this.#assertGeneration(generation); };
      assertCurrent();
      return work({
        assertCurrent,
        signal: AbortSignal.any([lease.signal, this.controlAbort.signal]),
        status: () => this.#statusLocked(),
        reconcile: () => this.#reconcileLocked(generation),
        setMode: (mode, options) => this.#setModeLocked(mode, options, generation),
      });
    })));
  }

  subscribe(listener) { this.changeListeners.add(listener); return () => this.changeListeners.delete(listener); }

  changed() { for (const listener of this.changeListeners) listener(); }

  allowsFullCapture() {
    return this.captureScope.isOpen && this.phase === 'full' && this.state.mode === 'full';
  }

  async captureState() {
    const { tabs, drops, drop_sources } = this.observer.snapshot();
    const counts = { armed: 0, frozen: 0, discarded: 0 };
    let socket = false;
    this.reportDrops ??= { expired: 0, rejected: 0 };
    this.reportDocuments ??= new Map();
    const activeDocuments = new Set(tabs.map((tab) => tab.id));
    for (const tab of tabs) {
      if (tab.discarded) { counts.discarded++; continue; }
      if (tab.frozen === true) { counts.frozen++; continue; }
      if (tab.status?.active === true && tab.status.mode === 'full') counts.armed++;
      socket ||= tab.status?.active === true && tab.status.forwarding === true && tab.status.ws2_socket_open === true;
    }
    const reason = this.state.mode === 'paused' ? 'paused'
      : this.state.mode === 'off' ? 'capture_off'
      : this.state.mode !== 'full' ? 'consent_needed'
      : this.phase === 'identity' ? 'hook_not_armed'
      : this.phase !== 'full' ? 'storage_locked'
      : this.runtime.configuration?.activeDocument?.history_acquisition?.enabled === false ? 'paused'
      : tabs.length === 0 ? 'no_onlyfans_tab'
      : counts.discarded === tabs.length ? 'tab_discarded'
      : counts.frozen + counts.discarded === tabs.length ? 'tab_frozen'
      : counts.armed === 0 ? 'hook_not_armed'
      : !this.captureScope.isOpen ? 'paused'
      : !socket ? 'page_socket_closed' : 'ok';
    return { observing: reason === 'ok', reason, tabs: counts, page_socket_open: socket,
      runnable: !['paused', 'capture_off', 'consent_needed', 'account_mismatch', 'storage_locked',
        'no_onlyfans_tab', 'tab_discarded', 'tab_frozen'].includes(reason), drops,
      drop_tabs: [...activeDocuments], drop_sources };
  }

  async #hasOnlyFansPermission() {
    return this.chromeApi.permissions.contains({ origins: [ONLYFANS_ORIGIN_PATTERN] });
  }

  async #hasHistoryPermission() {
    return this.chromeApi.permissions.contains({ permissions: ['webRequest'] });
  }

  async #hasLocalAnalyticsPermission() {
    // The companion is reached only through the packaged WebSocket origin;
    // it requires CSP authorization, not local HTTP host access.
    return true;
  }

  async #hasBrainBinding() {
    try {
      await this.adapter.loadBrainBinding({ signal: this.controlAbort.signal });
      return true;
    } catch (_error) {
      return false;
    }
  }

  async #desiredPhase() {
    if (!ACTIVE_CONSENT_MODES.has(this.state.mode)) return this.state.mode;
    if (!await this.#hasOnlyFansPermission()) return 'permission_required';
    if (this.state.mode === 'preview') return 'preview';
    if (!await this.#hasLocalAnalyticsPermission()) return 'permission_required';
    return await this.#hasBrainBinding() ? 'full' : 'identity';
  }

  async #onlyFansTabs() {
    try {
      return await this.chromeApi.tabs.query({ url: [ONLYFANS_ORIGIN_PATTERN] });
    } catch (_error) {
      return [];
    }
  }

  async #stopTabs(tabs) {
    return this.#controlTabs(tabs, 'stop');
  }

  async #controlTabs(tabs, action) {
    await Promise.all(tabs.map(async (tab) => {
      if (!Number.isInteger(tab.id)) return;
      let timer;
      const expired = Symbol('control_timeout');
      try {
        const response = await Promise.race([
          this.chromeApi.tabs.sendMessage(tab.id, {
            type: PAGE_CONTROL_MESSAGE_TYPE, version: PAGE_CONTROL_VERSION, action,
          }, { frameId: 0 }),
          new Promise((resolve) => { timer = this.scheduler.setTimeout(() => resolve(expired), 1_000); }),
        ]);
        if (action === 'pause' && response !== expired && response?.ok !== true) {
          await this.#controlTabs([tab], 'stop');
        }
      } catch (_error) {
        if (action === 'pause') await this.#controlTabs([tab], 'stop');
      } finally {
        if (timer !== undefined) this.scheduler.clearTimeout(timer);
      }
    }));
  }

  async #syncContentScripts(mode) {
    if (this.state.mode === 'paused' && ACTIVE_CONSENT_MODES.has(this.state.resume_mode)
      && await this.#hasOnlyFansPermission()) mode = this.state.resume_mode;
    const desired = contentScriptsFor(mode);
    this.scriptMode = mode;
    const registered = await this.chromeApi.scripting.getRegisteredContentScripts();
    const owned = registered.filter((entry) => entry.id.startsWith('ofca-'));
    if (!mode) await this.observer.configure(null);
    if (!sameScripts(owned, desired)) {
      if (owned.length) await this.chromeApi.scripting.unregisterContentScripts({ ids: owned.map((entry) => entry.id) });
      if (desired.length) await this.chromeApi.scripting.registerContentScripts(desired);
    }
    // Register future documents before attaching to existing ones or opening the
    // single inactive helper. Definitions changing never requires navigation.
    if (this.documentReset) await this.#controlTabs(await this.#onlyFansTabs(), 'reset_identity');
    if (mode) await this.observer.configure(mode, this.state.mode === 'paused');
    this.documentReset = false;
    this.identityRefreshPending = false;
  }

  async #suspendRuntime() {
    if (typeof this.runtime.history?.reportCaptureState === 'function') {
      try { await this.runtime.history.reportCaptureState(); } catch {}
    }
    if (typeof this.runtime.suspend === 'function') {
      await this.runtime.suspend();
      return;
    }
    this.runtime.history?.stop?.();
    this.runtime.transport?.stop?.();
  }

  async #applyPhase(desired, generation = this.controlGeneration) {
    const priorPhase = this.phase;
    this.phase = 'transitioning';

    if (desired !== 'full') await this.#suspendRuntime();

    let effective = desired;
    if (desired === 'full') {
      try {
        if (typeof this.runtime.start === 'function') await this.runtime.start();
        else await this.runtime.wake();
      } catch (_error) {
        effective = 'identity';
      }
    }

    this.#assertGeneration(generation);
    this.phase = effective;
    const scriptMode = SCRIPT_MODES.includes(effective) ? effective : null;
    try {
      await this.#syncContentScripts(scriptMode);
      this.#assertGeneration(generation);
    } catch (error) {
      this.phase = ACTIVE_CONSENT_MODES.has(desired)
        ? (priorPhase === 'booting' ? 'unavailable' : priorPhase)
        : 'unavailable';
      throw error;
    }
  }

  async #reconcileLocked(generation) {
    this.#assertGeneration(generation);
    this.captureScope.close('capture_reconcile');
    if (ACTIVE_CONSENT_MODES.has(this.state.mode)
      && !await this.activeModeAuthorization.reconcileActiveMode({
        mode: this.state.mode, state: structuredClone(this.state),
      })) {
      this.#assertGeneration(generation);
      this.state = {
        ...this.state,
        mode: 'paused',
        resume_mode: this.state.mode,
        consent_epoch: crypto.randomUUID(),
        updated_at: this.now().toISOString(),
      };
      await this.chromeApi.storage.local.set({ [CONSENT_STORAGE_KEY]: this.state });
    }
    const desired = await this.#desiredPhase();
    this.#assertGeneration(generation);
    await this.captureScope.drain();
    this.#assertGeneration(generation);
    await this.#applyPhase(desired, generation);
    this.#assertGeneration(generation);
    if (['preview', 'full'].includes(this.phase)) this.captureScope.reopen();
    void this.runtime.history?.wake?.('observing')?.catch(() => undefined);
    await this.#scheduleBindingRetry(generation);
    this.changed();
  }

  async #scheduleBindingRetry(generation) {
    if (generation !== this.controlGeneration || this.phase !== 'identity'
      || this.state.mode !== 'full' || this.bindingRetryTimer !== null || this.bindingRetryAttempt >= 9) return;
    let paired = false;
    try { paired = await this.hasSavedPairing(); } catch { /* No confirmed pairing. */ }
    // A storage event or consent transition may invalidate us during the status read.
    if (!paired || generation !== this.controlGeneration) return;
    this.bindingRetryAttempt += 1;
    const delay = this.bindingRetryDelay;
    this.bindingRetryDelay = Math.min(delay * 2, 30_000);
    this.bindingRetryTimer = this.scheduler.setTimeout(() => {
      if (generation !== this.controlGeneration) return;
      this.bindingRetryTimer = null;
      return this.controlQueue.run(async () => {
        if (generation !== this.controlGeneration) return;
        await this.#refreshTabIdentity(generation);
        if (generation !== this.controlGeneration) return;
        // Keep identity capture available while Brain is offline. Do not invalidate
        // the companion connection that this probe is trying to establish.
        const bound = await this.#hasBrainBinding();
        if (generation !== this.controlGeneration) return;
        if (bound) await this.#reconcileLocked(generation);
        else await this.#scheduleBindingRetry(generation);
      }).catch(() => undefined);
    }, delay);
  }

  async #refreshTabIdentity(generation) {
    if (!await this.#hasOnlyFansPermission()) return;
    const tabs = await this.#onlyFansTabs();
    if (generation !== this.controlGeneration || this.state.mode !== 'full') return;
    await Promise.all(tabs.map(async (tab) => {
      if (!Number.isInteger(tab.id) || tab.frozen || tab.discarded) return;
      let timer;
      try {
        // Request the live page's observation, never restore a cached account.
        // An unresponsive document cannot block Pause or the next bounded retry.
        await Promise.race([
          this.chromeApi.tabs.sendMessage(tab.id, {
            type: PAGE_CONTROL_MESSAGE_TYPE,
            version: PAGE_CONTROL_VERSION,
            action: 'refresh_identity',
          }, { frameId: 0 }),
          new Promise((resolve) => { timer = this.scheduler.setTimeout(resolve, 2_000); }),
        ]);
      } catch (_error) {
        // Attachment is reconciled by the observer owner; a timeout is not a diagnosis.
      } finally {
        if (timer !== undefined) this.scheduler.clearTimeout(timer);
      }
    }));
  }

  reconcile() {
    const generation = this.#invalidate('consent_reconcile');
    return this.controlQueue.run(async () => {
      await this.#loadStateLocked();
      return this.#reconcileLocked(generation);
    });
  }

  setMode(mode, options = {}) {
    this.register();
    const generation = this.#invalidate(`consent_${mode}`);
    return this.controlQueue.run(async () => {
      await this.#loadStateLocked();
      return this.#setModeLocked(mode, options, generation);
    });
  }

  async #setModeLocked(mode, { evidenceEventId = null } = {}, generation) {
    this.#assertGeneration(generation);
    const currentState = structuredClone(this.state);
    let nextMode = mode;
    let resumeMode = null;
    if (mode === 'pause') {
      if (!ACTIVE_CONSENT_MODES.has(this.state.mode)) {
        throw new Error('Only active analytics can be paused');
      }
      nextMode = 'paused';
      resumeMode = this.state.mode;
    } else if (mode === 'resume') {
      if (this.state.mode !== 'paused' || !ACTIVE_CONSENT_MODES.has(this.state.resume_mode)) {
        throw new Error('There is no paused consent to resume');
      }
      nextMode = this.state.resume_mode;
      const authorized = await this.activeModeAuthorization.authorizeResume({
        resumeMode: nextMode,
        currentState,
      });
      if (!authorized) throw new Error('Active analytics resume requires Legal mode-choice evidence');
    } else if (!['preview', 'full', 'revoked'].includes(mode)) {
      throw new Error('Unsupported consent transition');
    }

    if (ACTIVE_CONSENT_MODES.has(nextMode) && mode !== 'resume') {
      const authorized = await this.activeModeAuthorization.authorizeTransition({
        currentState,
        requestedMode: nextMode,
        evidenceEventId,
      });
      if (!authorized) throw new Error('Active analytics requires Legal mode-choice evidence');
    }

    if (ACTIVE_CONSENT_MODES.has(nextMode) && !await this.#hasOnlyFansPermission()) {
      throw new Error('OnlyFans access must be granted from setup');
    }
    if (nextMode === 'full' && !await this.#hasLocalAnalyticsPermission()) {
      throw new Error('Local analytics service access must be granted from setup');
    }
    this.#assertGeneration(generation);
    this.captureScope.close('consent_transition');
    await this.#suspendRuntime();
    await this.captureScope.drain();
    this.#assertGeneration(generation);
    const authorizationEventId = nextMode === 'revoked' ? null
      : evidenceEventId ?? this.state.authorization_event_id;
    const epochChanged = this.state.mode !== nextMode
      || this.state.authorization_event_id !== authorizationEventId;
    this.state = {
      schema: 'ofca-consent/v2',
      mode: nextMode,
      resume_mode: resumeMode,
      policy_revision: CONSENT_POLICY_REVISION,
      updated_at: this.now().toISOString(),
      authorization_event_id: authorizationEventId,
      consent_epoch: epochChanged ? crypto.randomUUID() : this.state.consent_epoch,
    };
    await this.chromeApi.storage.local.set({ [CONSENT_STORAGE_KEY]: this.state });
    if (epochChanged) {
      const fullFamily = (state) => state.mode === 'full'
        || (state.mode === 'paused' && state.resume_mode === 'full');
      // Mode changes retain a compatible observer; only an actual account
      // replacement resets document ownership.

      this.identityRefreshPending = !this.documentReset && nextMode === 'full';
      await this.provisioningIdentityBridge.clearContexts?.();
    }
    try {
      await this.#reconcileLocked(generation);
      if (epochChanged && nextMode === 'full') await this.#refreshTabIdentity(generation);
    } finally {
      if (nextMode === 'revoked') await this.#removePermissions();
    }
    return this.#statusLocked();
  }

  async #removePermissions() {
    const removed = await this.chromeApi.permissions.remove({
      permissions: ['webRequest'],
      origins: [ONLYFANS_ORIGIN_PATTERN],
    });
    if (removed === false) throw new Error('permission_remove_failed');
  }

  deleteLocalData() {
    this.deletionPending = true;
    this.legalScope.close('delete_requested');
    this.#invalidate('delete_requested');
    return this.controlQueue.run(async () => {
      await this.#deleteLocalDataLocked(true);
      this.loaded = true;
      this.initialization = Promise.resolve(this);
      return this.#statusLocked();
    });
  }

  async #deleteLocalDataLocked(createIntent) {
    this.captureScope.close('delete_requested');
    this.legalScope.close('delete_requested');
    this.deletionPending = true;
    this.state = defaultConsentState();
    const stored = await this.chromeApi.storage.local.get([DELETE_INTENT_KEY]);
    const intent = stored?.[DELETE_INTENT_KEY] ?? deletionIntent(this.now());
    await this.chromeApi.storage.local.set({
      [CONSENT_STORAGE_KEY]: this.state,
      ...(createIntent || !Object.hasOwn(stored ?? {}, DELETE_INTENT_KEY) ? { [DELETE_INTENT_KEY]: intent } : {}),
    });
    const failures = [];
    const attempt = async (stage, work) => {
      try { await work(); } catch { failures.push(stage); }
    };
    await attempt('phase_stop', () => this.#applyPhase('off'));
    await attempt('runtime_suspend', () => this.#suspendRuntime());
    await attempt('context_clear', () => this.provisioningIdentityBridge.clearContexts?.());
    await attempt('capture_drain', () => this.captureScope.drain());
    await attempt('legal_drain', () => this.legalScope.drain());
    await attempt('preview_drain', () => this.previewMetrics.drain?.());
    await attempt('evidence_close', () => this.activationEvidenceStore?.close());
    await attempt('permission_remove', () => this.#removePermissions());
    await attempt('binding_clear', () => this.adapter.clearBrainBinding());
    await attempt('local_data_clear', () => this.clearLocalData());
    if (failures.length > 0) {
      // Even a legacy/injected cleaner must not erase an incomplete deletion's intent.
      await this.chromeApi.storage.local.set({ [DELETE_INTENT_KEY]: intent });
      throw Object.assign(new Error('delete_incomplete'), { code: 'delete_incomplete', failures });
    }
    await this.chromeApi.storage.local.remove([DELETE_INTENT_KEY]);
    this.deletionPending = false;
    this.legalScope.reopen();
  }

  async #brainReachable() {
    if (typeof this.fetchImpl !== 'function') return false;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 750);
    try {
      const response = await this.fetchImpl(LOCAL_ANALYTICS_HEALTH_URL, {
        cache: 'no-store',
        signal: controller.signal,
      });
      return response.ok;
    } catch (_error) {
      return false;
    } finally {
      clearTimeout(timeout);
    }
  }

  async status() {
    if (!this.loaded) await this.initialize();
    return this.controlQueue.run(() => this.#statusLocked());
  }

  async #statusLocked() {
    const { tabs: onlyFansTabs, attached, helper } = this.observer.snapshot();
    const [preview, onlyFansPermission, localServicePermission, historyPermission] = await Promise.all([
      this.previewMetrics.summary(),
      this.#hasOnlyFansPermission(),
      this.#hasLocalAnalyticsPermission(),
      this.#hasHistoryPermission(),
    ]);
    const brainReachable = localServicePermission ? await this.#brainReachable() : false;
    const runtime = this.runtimeSummary() ?? {};
    return {
      schema: 'ofca-popup-status/v1',
      consent: structuredClone(this.state),
      phase: this.phase,
      reload_required: false,
      observer: { attachment: attached ? 'ready' : this.scriptMode ? 'checking' : 'blocked', helper },
      onlyfans_permission: onlyFansPermission,
      local_service_permission: localServicePermission,
      history_permission: historyPermission,
      brain_reachable: brainReachable,
      brain_bound: this.phase === 'full',
      preview,
      delivery: {
        startup_error_code: ['startup_failed', 'local_service_unavailable', 'local_service_timeout'].includes(runtime.startup_error_code)
          ? runtime.startup_error_code : null,
        history_error_code: ['history_unavailable', 'unsupported_browser', 'identity_required'].includes(runtime.history_error_code)
          ? runtime.history_error_code : null,
        browser_tab_sleeping: this.phase === 'full'
          && onlyFansTabs.some((tab) => Number.isInteger(tab?.id))
          && onlyFansTabs.every((tab) => Object.hasOwn(tab, 'frozen') && tab.frozen === true),
        capture_drop_counts: structuredClone(runtime.capture_drop_counts ?? {}),
        transport_state: ['authenticated', 'authenticating', 'disconnected'].includes(runtime.transport_state)
          ? runtime.transport_state : 'disconnected',
        runtime_ready: runtime.runtime_ready === true,
        socket_open: runtime.socket_open === true,
        pending_entries: Number.isSafeInteger(runtime.pending_entries)
          ? runtime.pending_entries
          : 0,
        captured_chats: Number.isSafeInteger(runtime.captured_chats)
          ? runtime.captured_chats
          : 0,
        captured_messages: Number.isSafeInteger(runtime.captured_messages)
          ? runtime.captured_messages
          : 0,
      },
    };
  }

  #onStorageChanged(changes, areaName) {
    if (areaName === 'session' && Object.hasOwn(changes, ACTIVE_ACCOUNT_PARTITION_KEY)) {
      const partition = changes[ACTIVE_ACCOUNT_PARTITION_KEY];
      // Publishing the first unsealed partition completes the current binding;
      // it is not an account replacement. Resetting document identity here
      // would lose the observation that authorized this same-browser pairing.
      if (partition && typeof partition === 'object' && !Array.isArray(partition)
        && typeof partition.newValue === 'string' && partition.newValue.length > 0
        && (partition.oldValue === undefined || partition.oldValue === partition.newValue)) return;
      this.documentReset = true;
      void this.reconcile().catch(() => undefined);
    }
  }

  #requestCaptureStateNotificationReport() {
    const requestReport = () => {
      const request = this.runtime.history?.requestCaptureStateReport?.();
      if (request && typeof request.catch === 'function') void request.catch(() => undefined);
    };
    const current = this.now().getTime();
    const windowMs = 5_000;
    if (this.captureNotificationLastReportAt === null
      || current - this.captureNotificationLastReportAt >= windowMs) {
      this.captureNotificationLastReportAt = current;
      this.captureNotificationPending = false;
      if (this.captureNotificationTimer !== null) this.scheduler.clearTimeout(this.captureNotificationTimer);
      this.captureNotificationTimer = null;
      requestReport();
      return;
    }
    this.captureNotificationPending = true;
    if (this.captureNotificationTimer !== null) return;
    const delay = Math.max(0, this.captureNotificationLastReportAt + windowMs - current);
    this.captureNotificationTimer = this.scheduler.setTimeout(() => {
      this.captureNotificationTimer = null;
      if (!this.captureNotificationPending) return;
      this.captureNotificationPending = false;
      this.captureNotificationLastReportAt = this.now().getTime();
      requestReport();
    }, delay);
  }

  #onMessage(message, sender, sendResponse) {
    if (message?.type === OBSERVER_STATE_TYPE) { this.observer.observe(message, sender); return false; }
    if (message?.type === 'ofca.capture.state.changed'
      || message?.type === 'ofca.capture.queue.changed') {
      if (Object.keys(message).length !== 1 || !trustedContentSender(sender, this.chromeApi)) return false;
      if (Number.isInteger(sender.tab?.id)) void this.observer.refreshDrops(sender.tab.id);
      this.#requestCaptureStateNotificationReport();
      return false;
    }
    if (message?.type === CAPTURE_STATE_QUERY_TYPE && Object.keys(message).length === 1) {
      if (!trustedContentSender(sender, this.chromeApi)) return false;
      const ready = this.loaded && this.phase !== 'booting' ? Promise.resolve() : this.initialize();
      void ready.then(() => sendResponse({
        ok: true, mode: this.state.mode, consent_epoch: this.state.consent_epoch,
      }), () => sendResponse({ ok: false }));
      return true;
    }
    if (message?.type === 'ofca.preview.delivery') {
      if (!trustedContentSender(sender, this.chromeApi)) return false;
      const generation = this.controlGeneration;
      void this.initialize().then(async () => {
        this.#assertGeneration(generation);
        if (!['preview', 'full'].includes(this.phase) || Object.keys(message).length !== 3
          || message.consent_epoch !== this.state.consent_epoch || !isPreviewEnvelope(message.envelope)) {
          sendResponse({ ok: false });
          return;
        }
        await this.captureScope.run(({ assertCurrent }) => this.previewMetrics.record(
          message.envelope.observation, { assertCurrent },
        ));
        sendResponse({ ok: true });
      }).catch(() => sendResponse({ ok: false }));
      return true;
    }

    if (!allowsUiMessage(sender, message, this.chromeApi)) return false;
    if (message?.type === UI_RELOAD_TABS_MESSAGE_TYPE) {
      sendResponse({ ok: false, code: 'reload_unsupported' });
      return false;
    }
    if (message?.type === OBSERVER_REOPEN_TYPE && Object.keys(message).length === 1) {
      void this.observer.reopen().then(() => this.status()).then(
        (status) => sendResponse({ ok: true, status }),
        () => sendResponse({ ok: false, code: 'attachment_unavailable' }),
      );
      return true;
    }
    if (message?.type === UI_STATUS_MESSAGE_TYPE && Object.keys(message).length === 1) {
      void this.status().then(
        (status) => sendResponse({ ok: true, status }),
        () => sendResponse({ ok: false, code: 'status_unavailable' }),
      );
      return true;
    }
    if (
      message?.type === UI_TRANSITION_MESSAGE_TYPE
      && Object.keys(message).length === 2
      && typeof message.mode === 'string'
    ) {
      void this.setMode(message.mode).then(
        (status) => sendResponse({ ok: true, status }),
        () => sendResponse({ ok: false, code: 'transition_rejected' }),
      );
      return true;
    }
    if (message?.type === UI_CLEAR_PREVIEW_MESSAGE_TYPE && Object.keys(message).length === 1) {
      void this.runLegalOperation(async ({ status }) => {
        await this.previewMetrics.clear();
        return status();
      }).then(
        (status) => sendResponse({ ok: true, status }),
        () => sendResponse({ ok: false, code: 'clear_failed' }),
      );
      return true;
    }
    if (message?.type === UI_DELETE_LOCAL_DATA_MESSAGE_TYPE && Object.keys(message).length === 1) {
      void this.deleteLocalData().then(
        (status) => sendResponse({ ok: true, status }),
        () => sendResponse({ ok: false, code: 'delete_failed' }),
      );
      return true;
    }
    return false;
  }
}
