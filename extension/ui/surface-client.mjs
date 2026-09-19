import { UI_STATUS_MESSAGE_TYPE } from '../runtime/consent-controller.mjs';
import { LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE } from '../runtime/legal-activation-controller.mjs';
import { UI_OPEN_SURFACE_MESSAGE_TYPE } from '../runtime/ui-surfaces.mjs';
import { customerReleaseConfig } from '../runtime/customer-release-config.mjs';
import { probeDesktopRuntime } from '../runtime/customer-journey.mjs';
import { LOCAL_SERVICE_ORIGIN, assertLocalServiceUrl } from '../transport/local-service-endpoints.mjs';

const unknownReadiness = () => ({ commercial_authority: 'unknown', analysis_admission: 'blocked' });
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

export function openSurface(surface, section = '') {
  return send({ type: UI_OPEN_SURFACE_MESSAGE_TYPE, surface, section });
}

// Only presentation lives here. Credentials, evidence and capture stay in the worker.
export function createSurfaceClient(onChange, onError) {
  const model = {
    status: null, legal: null, pairing: { state: 'unpaired', comparison_code: null, owns_attempt: false },
    desktopRuntimeReachable: false, analysisReadiness: unknownReadiness(),
    config: { dashboard_url: `${LOCAL_SERVICE_ORIGIN}/`, history_settings_url: `${LOCAL_SERVICE_ORIGIN}/settings`,
      desktop_app_download_url: customerReleaseConfig.desktop_app_download_url },
  };
  let port = null, stopped = false, refreshPromise = null, readinessTimer = null;
  let interval = null, reconnectTimer = null, readinessPending = false;
  let pendingCommand = null;
  const emit = () => { if (!stopped) onChange(model); };
  function resetReadiness() {
    clearTimeout(readinessTimer);
    readinessPending = false;
    model.analysisReadiness = unknownReadiness();
  }
  function requestReadiness() {
    if (!port || readinessPending || model.status?.consent.mode !== 'full'
      || !model.desktopRuntimeReachable || model.pairing.state !== 'paired') return;
    readinessPending = true;
    readinessTimer = setTimeout(() => {
      model.analysisReadiness = { commercial_authority: 'unavailable', analysis_admission: 'blocked' };
      // A replacement port cannot accept a late result from the timed-out request.
      const expired = port; port = null; expired?.disconnect(); readinessPending = false;
      emit(); reconnectTimer = setTimeout(connectPort, 1000);
    }, 12000);
    post('readiness');
  }
  function connectPort() {
    if (stopped || port) return;
    const connected = chrome.runtime.connect({ name: 'ofca.companion.pairing' });
    port = connected;
    connected.onMessage.addListener((value) => {
      if (stopped || port !== connected) return;
      if (value?.type === 'pairing_command_result') {
        if (pendingCommand?.port === connected && value.command === pendingCommand.command) {
          const waiting = pendingCommand; pendingCommand = null; clearTimeout(waiting.timer);
          if (value.ok === true) waiting.resolve(); else waiting.reject(new Error('command_failed'));
        }
        return;
      }
      if (value?.type === 'analysis_readiness') {
        if (!readinessPending) return;
        clearTimeout(readinessTimer); readinessPending = false;
        const valid = ['required', 'active', 'unavailable'].includes(value.commercial_authority)
          && ['blocked', 'admitted'].includes(value.analysis_admission)
          && !(value.analysis_admission === 'admitted' && value.commercial_authority !== 'active');
        model.analysisReadiness = valid && model.status?.consent.mode === 'full' && model.pairing.state === 'paired'
          ? { commercial_authority: value.commercial_authority, analysis_admission: value.analysis_admission }
          : { commercial_authority: 'unavailable', analysis_admission: 'blocked' };
      } else if (typeof value?.state === 'string') {
        model.pairing = { state: value.state, owns_attempt: value.owns_attempt === true,
          comparison_code: /^\d{6}$/u.test(value.comparison_code ?? '') ? value.comparison_code : null };
        if (value.state !== 'paired') resetReadiness();
        else requestReadiness();
      }
      emit();
    });
    connected.onDisconnect.addListener(() => {
      if (port !== connected || stopped) return;
      port = null; resetReadiness(); cancelPendingCommand();
      model.pairing = { state: 'unavailable', comparison_code: null, owns_attempt: false };
      model.desktopRuntimeReachable = false;
      emit(); reconnectTimer = setTimeout(() => { connectPort(); void refresh(); }, 1000);
    });
  }
  function post(type) {
    if (!port) throw new NoticeError('The extension is reconnecting. Try again.');
    port.postMessage({ type });
  }
  function command(type) {
    if (type !== 'forget' || !port || pendingCommand) return Promise.reject(new Error('command_unavailable'));
    return new Promise((resolve, reject) => {
      const waiting = { command: type, port, resolve, reject, timer: null };
      waiting.timer = setTimeout(() => {
        if (pendingCommand === waiting) pendingCommand = null;
        waiting.port.disconnect();
        reject(new NoticeError('The connection change could not be confirmed. Check its status before trying again.'));
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
    try {
      const [status, legal] = await Promise.all([
        send({ type: UI_STATUS_MESSAGE_TYPE }), send({ type: LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE }),
      ]);
      if (stopped) return;
      if (status?.consent?.mode !== 'full' || status.consent.consent_epoch !== model.status?.consent?.consent_epoch
        || status.delivery?.transport_state !== 'authenticated') resetReadiness();
      model.status = status; model.legal = legal;
      model.desktopRuntimeReachable = status.consent.mode === 'full' ? await probeDesktopRuntime() : false;
      if (!model.desktopRuntimeReachable) resetReadiness();
      if (port && status.consent.mode === 'full') post('status');
      requestReadiness(); emit();
    } catch (error) {
      model.status = null; model.legal = null; resetReadiness();
      model.pairing = { state: 'unavailable', comparison_code: null, owns_attempt: false };
      emit(); if (!stopped) onError(error);
    }
  }
  function refresh() {
    if (stopped) return Promise.resolve();
    if (!refreshPromise) refreshPromise = readCurrent().finally(() => { refreshPromise = null; });
    return refreshPromise;
  }
  async function sync() { await refreshPromise; return refresh(); }
  const visible = () => { if (document.visibilityState === 'visible') void refresh(); };
  const changed = (_changes, area) => { if (area === 'local') visible(); };
  async function start() {
    try {
      const value = await (await fetch(chrome.runtime.getURL('extension-config.json'))).json();
      if (value.schema === 'ofca-extension-config/v1') {
        model.config = { ...model.config,
          dashboard_url: assertLocalServiceUrl(value.dashboard_url).href,
          history_settings_url: assertLocalServiceUrl(value.history_settings_url).href };
      }
    } catch { /* Keep the pinned same-computer destinations. */ }
    connectPort(); await refresh();
    if (stopped) return;
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('focus', visible);
    chrome.storage.onChanged.addListener(changed);
    interval = setInterval(visible, 3000);
    window.addEventListener('pagehide', stop, { once: true });
  }
  function stop() {
    stopped = true; clearInterval(interval); clearTimeout(reconnectTimer); clearTimeout(readinessTimer);
    port?.disconnect(); port = null; cancelPendingCommand();
    document.removeEventListener('visibilitychange', visible); window.removeEventListener('focus', visible);
    chrome.storage.onChanged.removeListener(changed);
  }
  return { model, start, stop, refresh, sync, post, command };
}
