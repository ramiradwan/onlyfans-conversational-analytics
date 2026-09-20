import { allowsUiMessage } from './ui-surfaces.mjs';
import { OperationScope, SerialExecutor } from './operation-scope.mjs';
import { DELETE_INTENT_KEY, deletionIntent } from './deletion-state.mjs';
import { LOCAL_SERVICE_PATTERN, LOCAL_SERVICE_HEALTH } from '../transport/local-service-endpoints.mjs';
import {
  PAGE_CONTROL_MESSAGE_TYPE,
  PAGE_CONTROL_VERSION,
  PREVIEW_MESSAGE_TYPE,
  isPreviewEnvelope,
} from '../capture/envelopes.mjs';

export const CONSENT_STORAGE_KEY = 'ofca_consent_v1';
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
    this.reloadRequired = false;
    this.documentReset = false;
    this.state = defaultConsentState();
    this.phase = 'booting';
    this.initialization = null;
    this.registered = false;
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
    this.chromeApi.runtime.onMessage.addListener(this.messageListener);
    this.chromeApi.storage.onChanged?.addListener(this.storageListener);
    this.chromeApi.permissions.onRemoved?.addListener(this.permissionListener);
    this.chromeApi.permissions.onAdded?.addListener(this.permissionListener);
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
    this.adapter.invalidate?.();
    this.controlAbort.abort(Object.assign(new Error(code), { code }));
    this.controlAbort = new AbortController();
    this.captureScope.close(code);
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
        setMode: (mode, options) => this.#setModeLocked(mode, options, generation),
      });
    })));
  }

  allowsFullCapture() {
    return this.captureScope.isOpen && this.phase === 'full' && this.state.mode === 'full';
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
    await Promise.all(tabs.map(async (tab) => {
      if (!Number.isInteger(tab.id)) return;
      try {
        await this.chromeApi.tabs.sendMessage(tab.id, {
          type: PAGE_CONTROL_MESSAGE_TYPE,
          action: 'stop',
        });
      } catch (_error) {
        // A tab without the content bridge is already stopped.
      }
    }));
  }

  async #reloadTabs(tabs) {
    await Promise.all(tabs.map(async (tab) => {
      if (!Number.isInteger(tab.id)) return;
      let timer;
      try {
        // Edge may freeze a background document before acknowledging reload.
        // The user requested it once; never retry it or hold the shared setup
        // queue until that tab is brought to the foreground.
        await Promise.race([
          this.chromeApi.tabs.reload(tab.id),
          new Promise((resolve) => { timer = this.scheduler.setTimeout(resolve, 2_000); }),
        ]);
      } catch (_error) {
        // A tab closed during the transition needs no further action.
      } finally {
        if (timer !== undefined) this.scheduler.clearTimeout(timer);
      }
    }));
  }

  async #syncContentScripts(mode) {
    const desired = contentScriptsFor(mode);
    const allRegistered = await this.chromeApi.scripting.getRegisteredContentScripts();
    const owned = allRegistered.filter((entry) => entry.id.startsWith('ofca-'));
    // A transient companion refusal must not stop the live document bridge:
    // that bridge is how a restarted worker can re-observe account identity.
    // Capture admission stays closed until authenticated Full startup succeeds.
    if (mode === 'identity' && this.state.mode === 'full' && !this.documentReset
      && sameScripts(owned, contentScriptsFor('full'))) {
      let paired = false;
      try { paired = await this.hasSavedPairing(); } catch { /* No confirmed pairing. */ }
      if (paired) return;
    }
    const definitionsChanged = !sameScripts(owned, desired);
    if (!definitionsChanged && !this.documentReset) return;

    const tabs = await this.#onlyFansTabs();
    await this.#stopTabs(tabs);
    if (definitionsChanged && owned.length > 0) {
      await this.chromeApi.scripting.unregisterContentScripts({
        ids: owned.map((entry) => entry.id),
      });
    }
    if (definitionsChanged && desired.length > 0) {
      await this.chromeApi.scripting.registerContentScripts(desired);
    }
    this.reloadRequired = desired.length > 0 && tabs.length > 0;
    this.documentReset = false;
  }

  async #suspendRuntime() {
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
    await this.#scheduleBindingRetry(generation);
  }

  async #scheduleBindingRetry(generation) {
    if (generation !== this.controlGeneration || this.phase !== 'identity'
      || this.state.mode !== 'full' || this.bindingRetryTimer !== null) return;
    let paired = false;
    try { paired = await this.hasSavedPairing(); } catch { /* No confirmed pairing. */ }
    // A storage event or consent transition may invalidate us during the status read.
    if (!paired || generation !== this.controlGeneration) return;
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
        // A missing bridge requires the existing explicit reload action.
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
      throw new Error('OnlyFans access must be granted from the popup');
    }
    if (nextMode === 'full' && !await this.#hasLocalAnalyticsPermission()) {
      throw new Error('Local analytics service access must be granted from the popup');
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
      this.documentReset = true;
      await this.provisioningIdentityBridge.clearContexts?.();
    }
    try {
      await this.#reconcileLocked(generation);
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
      reload_required: this.reloadRequired,
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
    if (areaName === 'session' && Object.hasOwn(changes, 'active_account_partition_v5')) {
      void this.reconcile().catch(() => undefined);
    }
  }

  #onMessage(message, sender, sendResponse) {
    if (message?.type === PREVIEW_MESSAGE_TYPE) {
      if (!trustedContentSender(sender, this.chromeApi)) return false;
      const generation = this.controlGeneration;
      void this.initialize().then(async () => {
        this.#assertGeneration(generation);
        if (!['preview', 'full'].includes(this.phase) || !isPreviewEnvelope(message)) {
          sendResponse({ ok: false });
          return;
        }
        await this.captureScope.run(({ assertCurrent }) => this.previewMetrics.record(
          message.observation, { assertCurrent },
        ));
        sendResponse({ ok: true });
      }).catch(() => sendResponse({ ok: false }));
      return true;
    }

    if (!allowsUiMessage(sender, message, this.chromeApi)) return false;
    if (message?.type === UI_RELOAD_TABS_MESSAGE_TYPE && Object.keys(message).length === 1) {
      void this.runLegalOperation(async ({ assertCurrent, status }) => {
        assertCurrent();
        if (SCRIPT_MODES.includes(this.phase)) {
          await this.#reloadTabs(await this.#onlyFansTabs());
          this.reloadRequired = false;
        }
        return status();
      }).then(
        (status) => sendResponse({ ok: true, status }),
        () => sendResponse({ ok: false, code: 'reload_failed' }),
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
