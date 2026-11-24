import { UI_STATUS_MESSAGE_TYPE } from '../runtime/consent-controller.mjs';
import { LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE } from '../runtime/legal-activation-controller.mjs';
import { UI_OPEN_SURFACE_MESSAGE_TYPE } from '../runtime/ui-surfaces.mjs';
import { customerReleaseConfig } from '../runtime/customer-release-config.mjs';
import { probeDesktopRuntime } from '../runtime/customer-journey.mjs';
import { LOCAL_SERVICE_ORIGIN, assertLocalServiceUrl } from '../transport/local-service-endpoints.mjs';
import { DESKTOP_LINK_STORAGE_KEY } from '../runtime/desktop-port.mjs';
import { isReadinessResponse, READINESS_REQUEST, READINESS_RESPONSE } from '../runtime/surface-readiness.mjs';
import { projectSupportChannels } from './support-channels.mjs';

const unknownReadiness = () => ({ commercial_authority: 'unknown', analysis_admission: 'blocked' });
export function isFullFamilyStatus(status) {
  return status?.consent?.mode === 'full'
    || (status?.consent?.mode === 'paused' && status.consent.resume_mode === 'full');
}
export class NoticeError extends Error {}
export const noticeText = (error, fallback) => error instanceof NoticeError ? error.message : fallback;

export function secureExternalUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.hostname.endsWith('.invalid') ? url.href : null;
  } catch { return null; }
}

export async function send(message) {
  let timer;
  try {
    const response = await Promise.race([
      chrome.runtime.sendMessage(message),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('runtime_timeout')), 12000); }),
    ]);
    if (response?.ok !== true) throw new Error('request_failed');
    return response.result ?? response.status;
  } finally { clearTimeout(timer); }
}

export function openSurface(surface, section = '', requestedFull = false) {
  return send({ type: UI_OPEN_SURFACE_MESSAGE_TYPE, surface, section, ...(requestedFull ? { requestedFull: true } : {}) });
}

// Only presentation lives here. Credentials, evidence and capture stay in the worker.
export function createSurfaceClient(onChange, onError) {
  const model = {
    status: null, legal: null, pairing: { state: 'unpaired', comparison_code: null, owns_attempt: false, desktop_attempt: false, desktop_control: false },
    desktopRuntimeReachable: false, desktopLinked: false, connectionRecovery: 'idle',
    analysisReadiness: unknownReadiness(),
    config: { dashboard_url: `${LOCAL_SERVICE_ORIGIN}/`, history_settings_url: `${LOCAL_SERVICE_ORIGIN}/settings`,
      desktop_app_download_url: customerReleaseConfig.desktop_app_download_url },
  };
  let port = null, connectionEpoch = 0, stopped = false;
  let refreshPromise = null, refreshRequested = false, reconnectTimer = null, readiness = null;
  let reconnectAttempts = 0, wakeAt = 0;
  const reconnectDelays = [200, 500, 1000];
  function reconnect() {
    if (stopped || reconnectTimer !== null) return;
    if (reconnectAttempts >= reconnectDelays.length) {
      model.connectionRecovery = 'exhausted';
      emit(); return;
    }
    model.connectionRecovery = 'retrying';
    emit();
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null; connectPort(); void refresh();
    }, reconnectDelays[reconnectAttempts++]);
  }
  let pendingCommand = null;
  const emit = () => { if (!stopped) onChange(model); };
  function resetReadiness() {
    if (readiness) clearTimeout(readiness.timer);
    readiness = null;
    model.analysisReadiness = unknownReadiness();
  }
  function readinessEligible() {
    return port && model.status?.consent?.mode === 'full'
      && model.desktopRuntimeReachable && model.pairing.state === 'paired';
  }
  function requestReadiness() {
    if (!readinessEligible() || readiness) return;
    const waiting = { port, epoch: model.status.consent.consent_epoch, id: crypto.randomUUID(), timer: null };
    waiting.timer = setTimeout(() => {
      if (readiness !== waiting || port !== waiting.port) return;
      losePort(waiting.port, true);
      waiting.port.disconnect();
    }, 12000);
    readiness = waiting;
    try { waiting.port.postMessage({ type: READINESS_REQUEST, request_id: waiting.id }); }
    catch {
      losePort(waiting.port, true);
      waiting.port.disconnect();
    }
  }
  function losePort(connected, readinessUnavailable = false) {
    if (port !== connected || stopped) return;
    port = null; connectionEpoch++;
    resetReadiness(); cancelPendingCommand();
    if (readinessUnavailable) model.analysisReadiness = { commercial_authority: 'unavailable', analysis_admission: 'blocked' };
    model.pairing = { state: 'unavailable', comparison_code: null, owns_attempt: false, desktop_attempt: false, desktop_control: false };
    model.desktopRuntimeReachable = false;
    reconnect();
  }
  function connectPort() {
    if (stopped || port) return;
    let connected;
    try { connected = chrome.runtime.connect({ name: 'ofca.companion.pairing' }); }
    catch { reconnect(); return; }
    port = connected; connectionEpoch++;
    connected.onMessage.addListener((value) => {
      if (stopped || port !== connected) return;
      if (value?.type === 'pairing_command_result') {
        if (pendingCommand?.port === connected && value.command === pendingCommand.command) {
          const waiting = pendingCommand; pendingCommand = null; clearTimeout(waiting.timer);
          if (value.ok === true) waiting.resolve(); else waiting.reject(new Error('command_failed'));
        }
        return;
      }
      if (value?.type === 'surface_changed') { void refresh(); return; }
      if (value?.type === READINESS_RESPONSE) {
        if (!readiness || readiness.port !== connected || value.request_id !== readiness.id
          || model.status?.consent?.consent_epoch !== readiness.epoch || !readinessEligible()) return;
        resetReadiness();
        model.analysisReadiness = isReadinessResponse(value)
          ? { commercial_authority: value.commercial_authority, analysis_admission: value.analysis_admission }
          : { commercial_authority: 'unavailable', analysis_admission: 'blocked' };
      } else if (typeof value?.state === 'string') {
        reconnectAttempts = 0; model.connectionRecovery = 'idle';
        model.pairing = { state: value.state, owns_attempt: value.owns_attempt === true,
          desktop_attempt: value.desktop_attempt === true,
          // True while the desktop app can control this browser over an open session.
          desktop_control: value.desktop_control === true,
          comparison_code: /^\d{6}$/u.test(value.comparison_code ?? '') ? value.comparison_code : null };
        if (value.state !== 'paired') resetReadiness();
        else requestReadiness();
      }
      emit();
    });
    connected.onDisconnect.addListener(() => losePort(connected));
  }

  function post(type) {
    if (!port) throw new NoticeError('The extension is not connected.');
    port.postMessage({ type });
  }
  function command(type) {
    if (type !== 'forget' || !port || pendingCommand) return Promise.reject(new Error('command_unavailable'));
    return new Promise((resolve, reject) => {
      const waiting = { command: type, port, resolve, reject, timer: null };
      waiting.timer = setTimeout(() => {
        if (pendingCommand === waiting) pendingCommand = null;
        waiting.port.disconnect();
        reject(new NoticeError('The connection change could not be confirmed.'));
      }, 12000);
      pendingCommand = waiting;
      try { post(type); } catch (error) { clearTimeout(waiting.timer); pendingCommand = null; reject(error); }
    });
  }
  function cancelPendingCommand() {
    if (!pendingCommand) return;
    clearTimeout(pendingCommand.timer); pendingCommand.reject(new Error('connection_lost')); pendingCommand = null;
  }
  async function readCurrent() {
    const connection = connectionEpoch;
    const current = () => !stopped && connection === connectionEpoch;
    try {
      const [status, legal, linked] = await Promise.all([
        send({ type: UI_STATUS_MESSAGE_TYPE }), send({ type: LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE }), desktopLinked(),
      ]);
      if (!current()) return;
      if (status?.consent?.mode !== 'full' || status.consent.consent_epoch !== model.status?.consent?.consent_epoch
        || status.delivery?.transport_state !== 'authenticated') resetReadiness();
      model.status = status; model.legal = legal; model.desktopLinked = linked;
      // Reachability is a fresh authenticated runtime fact. Pairing state
      // comes from the port and can change while a status read is in flight.
      model.desktopRuntimeReachable = isFullFamilyStatus(status)
        && status.brain_reachable === true && status.delivery?.transport_state === 'authenticated';
      if (!model.desktopRuntimeReachable) resetReadiness();
      if (port && isFullFamilyStatus(status)) post('status');
      requestReadiness(); emit();
    } catch (error) {
      if (!current()) return;
      model.status = null; model.legal = null; model.desktopRuntimeReachable = false;
      resetReadiness(); emit(); onError(error);
    }
  }
  function refresh() {
    if (stopped) return Promise.resolve();
    refreshRequested = true;
    if (!refreshPromise) refreshPromise = (async () => {
      try {
        do {
          refreshRequested = false;
          await readCurrent();
        } while (!stopped && refreshRequested);
      } finally { refreshPromise = null; }
    })();
    return refreshPromise;
  }
  async function sync() {
    await refreshPromise;
    if (model.connectionRecovery === 'exhausted' && !port) {
      reconnectAttempts = 0;
      model.connectionRecovery = 'retrying';
      emit();
      connectPort();
    }
    return refresh();
  }
  const visible = () => { if (document.visibilityState === 'visible') void refresh(); };
  const wake = () => {
    if (document.visibilityState !== 'visible' || Date.now() - wakeAt < 1000) return;
    wakeAt = Date.now();
    if (!port && reconnectTimer === null) {
      reconnectAttempts = 0; model.connectionRecovery = 'retrying'; emit(); connectPort();
    }
    visible();
  };
  const changed = (changes, area) => {
    if (area === 'local' || (area === 'session' && Object.hasOwn(changes, DESKTOP_LINK_STORAGE_KEY))) visible();
  };
  async function desktopLinked() {
    try {
      const stored = await chrome.storage.session.get([DESKTOP_LINK_STORAGE_KEY]);
      return Number.isSafeInteger(stored?.[DESKTOP_LINK_STORAGE_KEY]) && stored[DESKTOP_LINK_STORAGE_KEY] > 0;
    } catch { return false; }
  }
  async function start() {
    try {
      const value = await (await fetch(chrome.runtime.getURL('extension-config.json'))).json();
      if (value.schema === 'ofca-extension-config/v1') {
        model.config = { ...model.config,
          dashboard_url: assertLocalServiceUrl(value.dashboard_url).href,
          history_settings_url: assertLocalServiceUrl(value.history_settings_url).href,
          support_channels: projectSupportChannels(value.support_channels) };
      }
    } catch { /* Keep the pinned same-computer destinations. */ }
    connectPort(); await refresh();
    if (stopped) return;
    document.addEventListener('visibilitychange', wake);
    window.addEventListener('focus', wake);
    chrome.storage.onChanged.addListener(changed);
    window.addEventListener('pagehide', stop, { once: true });
  }
  function stop() {
    stopped = true; clearTimeout(reconnectTimer); resetReadiness();
    port?.disconnect(); port = null; cancelPendingCommand();
    document.removeEventListener('visibilitychange', wake); window.removeEventListener('focus', wake);
    chrome.storage.onChanged.removeListener(changed);
  }
  return { model, start, stop, refresh, sync, post, command };
}
