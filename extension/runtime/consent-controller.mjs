import {
  PAGE_CONTROL_MESSAGE_TYPE,
  PREVIEW_MESSAGE_TYPE,
  isPreviewEnvelope,
} from '../capture/envelopes.mjs';

export const CONSENT_STORAGE_KEY = 'ofca_consent_v1';
export const CONSENT_POLICY_REVISION = '1';
export const UI_STATUS_MESSAGE_TYPE = 'ofca.ui.status';
export const UI_TRANSITION_MESSAGE_TYPE = 'ofca.ui.transition';
export const UI_CLEAR_PREVIEW_MESSAGE_TYPE = 'ofca.ui.clear-preview';
export const UI_DELETE_LOCAL_DATA_MESSAGE_TYPE = 'ofca.ui.delete-local-data';
export const ONLYFANS_ORIGIN_PATTERN = 'https://onlyfans.com/*';
export const LOCAL_ANALYTICS_ORIGIN_PATTERN = 'http://bridge.localhost:17871/*';
export const LOCAL_ANALYTICS_HEALTH_URL = 'http://bridge.localhost:17871/health';
export const PREVIEW_PRUNE_ALARM_NAME = 'ofca-preview-retention';

const SCRIPT_MODES = Object.freeze(['identity', 'preview', 'full']);
const ACTIVE_CONSENT_MODES = new Set(['preview', 'full']);
const ALL_CONSENT_MODES = new Set(['off', 'preview', 'full', 'paused', 'revoked']);
const CONTENT_SCRIPT_IDS = Object.freeze(
  SCRIPT_MODES.flatMap((mode) => [`ofca-${mode}-main`, `ofca-${mode}-isolated`]),
);

function defaultState() {
  return {
    schema: 'ofca-consent/v1',
    mode: 'off',
    resume_mode: null,
    policy_revision: CONSENT_POLICY_REVISION,
    updated_at: null,
  };
}

function isTimestamp(value) {
  return typeof value === 'string' && Number.isFinite(Date.parse(value));
}

function validatedState(value) {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== 5
    || value.schema !== 'ofca-consent/v1'
    || !ALL_CONSENT_MODES.has(value.mode)
    || (
      value.resume_mode !== null
      && !ACTIVE_CONSENT_MODES.has(value.resume_mode)
    )
    || (value.updated_at !== null && !isTimestamp(value.updated_at))
    || typeof value.policy_revision !== 'string'
  ) return defaultState();

  const normalized = structuredClone(value);
  if (
    normalized.policy_revision !== CONSENT_POLICY_REVISION
    && ACTIVE_CONSENT_MODES.has(normalized.mode)
  ) {
    return {
      ...normalized,
      mode: 'paused',
      resume_mode: normalized.mode,
    };
  }
  return normalized;
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

function sameIds(registered, desired) {
  const left = registered.map((entry) => entry.id).sort();
  const right = desired.map((entry) => entry.id).sort();
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

function trustedContentSender(sender, chromeApi) {
  if (sender?.id !== chromeApi.runtime.id || sender?.frameId !== 0) return false;
  try {
    return new URL(sender.url).origin === 'https://onlyfans.com';
  } catch (_error) {
    return false;
  }
}

function trustedUiSender(sender, chromeApi) {
  return sender?.id === chromeApi.runtime.id
    && typeof sender?.url === 'string'
    && sender.url.startsWith(chromeApi.runtime.getURL(''));
}

export class ConsentController {
  constructor({
    chromeApi = globalThis.chrome,
    runtime,
    adapter,
    brainBindingBridge,
    provisioningIdentityBridge,
    previewMetrics,
    clearLocalData,
    runtimeSummary = () => ({}),
    fetchImpl = globalThis.fetch,
    now = () => new Date(),
  }) {
    if (!chromeApi?.storage?.local || !chromeApi?.scripting || !chromeApi?.permissions) {
      throw new Error('Consent controller requires Chrome storage, scripting, and permissions');
    }
    if (!runtime?.start && !runtime?.wake) throw new Error('Consent controller requires Agent runtime');
    if (typeof adapter?.loadBrainBinding !== 'function') {
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
    this.chromeApi = chromeApi;
    this.runtime = runtime;
    this.adapter = adapter;
    this.brainBindingBridge = brainBindingBridge;
    this.provisioningIdentityBridge = provisioningIdentityBridge;
    this.previewMetrics = previewMetrics;
    this.clearLocalData = clearLocalData;
    this.runtimeSummary = runtimeSummary;
    this.fetchImpl = fetchImpl;
    this.now = now;
    this.state = defaultState();
    this.phase = 'booting';
    this.initialization = null;
    this.transition = Promise.resolve();
    this.registered = false;
    this.messageListener = this.#onMessage.bind(this);
    this.storageListener = this.#onStorageChanged.bind(this);
    this.permissionListener = () => { void this.reconcile(); };
    this.alarmListener = (alarm) => {
      if (alarm?.name === PREVIEW_PRUNE_ALARM_NAME) {
        void this.previewMetrics.prune().catch(() => undefined);
      }
    };
  }

  register() {
    if (this.registered) return;
    this.chromeApi.runtime.onMessage.addListener(this.messageListener);
    this.chromeApi.storage.onChanged?.addListener(this.storageListener);
    this.chromeApi.permissions.onRemoved?.addListener(this.permissionListener);
    this.chromeApi.alarms?.onAlarm?.addListener(this.alarmListener);
    const alarm = this.chromeApi.alarms?.create?.(PREVIEW_PRUNE_ALARM_NAME, {
      delayInMinutes: 1,
      periodInMinutes: 24 * 60,
    });
    alarm?.catch?.(() => undefined);
    this.registered = true;
  }

  initialize() {
    if (this.initialization !== null) return this.initialization;
    this.register();
    this.initialization = (async () => {
      const saved = await this.chromeApi.storage.local.get([CONSENT_STORAGE_KEY]);
      this.state = validatedState(saved?.[CONSENT_STORAGE_KEY]);
      await this.previewMetrics.prune();
      await this.reconcile();
      return this;
    })();
    return this.initialization;
  }

  allowsFullCapture() {
    return this.phase === 'full' && this.state.mode === 'full';
  }

  async #hasOnlyFansPermission() {
    return this.chromeApi.permissions.contains({ origins: [ONLYFANS_ORIGIN_PATTERN] });
  }

  async #hasHistoryPermission() {
    return this.chromeApi.permissions.contains({ permissions: ['webRequest'] });
  }

  async #hasLocalAnalyticsPermission() {
    return this.chromeApi.permissions.contains({ origins: [LOCAL_ANALYTICS_ORIGIN_PATTERN] });
  }

  async #hasBrainBinding() {
    try {
      await this.adapter.loadBrainBinding();
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
      try {
        await this.chromeApi.tabs.reload(tab.id);
      } catch (_error) {
        // A tab closed during the transition needs no further action.
      }
    }));
  }

  async #syncContentScripts(mode) {
    const desired = contentScriptsFor(mode);
    const allRegistered = await this.chromeApi.scripting.getRegisteredContentScripts();
    const owned = allRegistered.filter((entry) => CONTENT_SCRIPT_IDS.includes(entry.id));
    if (sameIds(owned, desired)) return;

    const tabs = await this.#onlyFansTabs();
    await this.#stopTabs(tabs);
    if (owned.length > 0) {
      await this.chromeApi.scripting.unregisterContentScripts({
        ids: owned.map((entry) => entry.id),
      });
    }
    if (desired.length > 0) {
      await this.chromeApi.scripting.registerContentScripts(desired);
    }
    await this.#reloadTabs(tabs);
  }

  async #suspendRuntime() {
    if (typeof this.runtime.suspend === 'function') {
      await this.runtime.suspend();
      return;
    }
    this.runtime.history?.stop?.();
    this.runtime.transport?.stop?.();
  }

  async #applyPhase(desired) {
    const priorPhase = this.phase;
    this.phase = 'transitioning';

    if (desired !== 'full') await this.#suspendRuntime();
    if (['identity', 'full'].includes(desired)) {
      this.brainBindingBridge.register();
      this.provisioningIdentityBridge.register();
    } else {
      this.brainBindingBridge.unregister();
      this.provisioningIdentityBridge.unregister();
    }

    let effective = desired;
    if (desired === 'full') {
      try {
        if (typeof this.runtime.start === 'function') await this.runtime.start();
        else await this.runtime.wake();
      } catch (_error) {
        effective = 'identity';
      }
    }

    this.phase = effective;
    const scriptMode = SCRIPT_MODES.includes(effective) ? effective : null;
    try {
      await this.#syncContentScripts(scriptMode);
    } catch (error) {
      // A failed teardown must never leave the message gate in its former active
      // phase while a stale content-script registration is being retried.
      this.phase = ACTIVE_CONSENT_MODES.has(desired)
        ? (priorPhase === 'booting' ? 'unavailable' : priorPhase)
        : 'unavailable';
      throw error;
    }
  }

  reconcile() {
    const operation = this.transition.then(async () => {
      const desired = await this.#desiredPhase();
      await this.#applyPhase(desired);
    });
    this.transition = operation.catch(() => undefined);
    return operation;
  }

  async setMode(mode) {
    await this.initialize();
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
    } else if (!['preview', 'full', 'revoked'].includes(mode)) {
      throw new Error('Unsupported consent transition');
    }

    if (ACTIVE_CONSENT_MODES.has(nextMode) && !await this.#hasOnlyFansPermission()) {
      throw new Error('OnlyFans access must be granted from the popup');
    }
    if (nextMode === 'full' && !await this.#hasLocalAnalyticsPermission()) {
      throw new Error('Local analytics service access must be granted from the popup');
    }
    this.state = {
      schema: 'ofca-consent/v1',
      mode: nextMode,
      resume_mode: resumeMode,
      policy_revision: CONSENT_POLICY_REVISION,
      updated_at: this.now().toISOString(),
    };
    await this.chromeApi.storage.local.set({ [CONSENT_STORAGE_KEY]: this.state });
    try {
      await this.reconcile();
    } finally {
      if (nextMode === 'revoked') {
        await this.chromeApi.permissions.remove({
          permissions: ['webRequest'],
          origins: [ONLYFANS_ORIGIN_PATTERN, LOCAL_ANALYTICS_ORIGIN_PATTERN],
        });
      }
    }
    return this.status();
  }

  async deleteLocalData() {
    await this.initialize();
    const operation = this.transition.then(async () => {
      this.state = defaultState();
      let failure = null;
      try {
        await this.#applyPhase('off');
      } catch (error) {
        failure = error;
      }
      try {
        await this.chromeApi.permissions.remove({
          permissions: ['webRequest'],
          origins: [ONLYFANS_ORIGIN_PATTERN, LOCAL_ANALYTICS_ORIGIN_PATTERN],
        });
      } catch (error) {
        failure ??= error;
      }
      try {
        await this.clearLocalData();
      } catch (error) {
        failure ??= error;
      }
      this.state = defaultState();
      if (failure !== null) throw failure;
    });
    this.transition = operation.catch(() => undefined);
    await operation;
    return this.status();
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
    await this.initialize();
    await this.transition;
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
      onlyfans_permission: onlyFansPermission,
      local_service_permission: localServicePermission,
      history_permission: historyPermission,
      brain_reachable: brainReachable,
      brain_bound: this.phase === 'full',
      preview,
      delivery: {
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
    if (areaName === 'session' && Object.hasOwn(changes, 'active_account_partition_v4')) {
      void this.reconcile();
    }
  }

  #onMessage(message, sender, sendResponse) {
    if (message?.type === PREVIEW_MESSAGE_TYPE) {
      if (!trustedContentSender(sender, this.chromeApi)) return false;
      void this.initialize().then(async () => {
        if (!['preview', 'full'].includes(this.phase) || !isPreviewEnvelope(message)) {
          sendResponse({ ok: false });
          return;
        }
        await this.previewMetrics.record(message.observation);
        sendResponse({ ok: true });
      }).catch(() => sendResponse({ ok: false }));
      return true;
    }

    if (!trustedUiSender(sender, this.chromeApi)) return false;
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
      void this.previewMetrics.clear().then(
        () => this.status(),
      ).then(
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
