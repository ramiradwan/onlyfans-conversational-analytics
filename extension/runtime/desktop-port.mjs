// A browser-local channel from the desktop app's page to this extension.
//
// Chrome routes the port by extension ID and reports the page origin, so the
// desktop app gets pushed setup state without polling and without loopback
// traffic before pairing (ADR 0024, ADR 0045). The port carries no authority:
// it can read a coarse stage, open an extension-owned page, and start or cancel
// a pairing attempt that the port itself owns. Every state change still goes
// through the consent, legal, and companion controllers.
import { LOCAL_SERVICE_ORIGIN } from '../transport/local-service-endpoints.mjs';

export const DESKTOP_PORT_NAME = 'ofca.desktop';
export const DESKTOP_PORT_VERSION = 1;
export const DESKTOP_LINK_STORAGE_KEY = 'desktop_link_v1';
export const DESKTOP_STEPS = Object.freeze(['setup', 'access', 'history', 'connection', 'creator']);
export const DESKTOP_STAGES = Object.freeze([
  'unavailable', 'needs_terms', 'paused', 'needs_full', 'needs_site_access',
  'needs_account', 'ready_to_pair', 'pairing', 'paired',
]);
const OPEN_INTERVAL_MS = 1_000;

function exactKeys(value, keys) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

/** Admit only the top-level document of the local desktop app in a browser tab. */
export function admitDesktopSender(sender, origin = LOCAL_SERVICE_ORIGIN) {
  let senderOrigin = sender?.origin;
  if (typeof senderOrigin !== 'string') {
    try { senderOrigin = new URL(sender?.url).origin; } catch { return false; }
  }
  return senderOrigin === origin
    && sender?.id === undefined
    && sender?.frameId === 0
    && Number.isInteger(sender?.tab?.id) && sender.tab.id >= 0
    && Number.isInteger(sender?.tab?.windowId);
}

/** Derive the coarse setup stage. It names no account, code, or count. */
export function desktopStage({ consent, legal, pairing }) {
  if (!consent || !legal?.configured) return 'unavailable';
  const mode = consent.consent?.mode;
  const active = mode === 'preview' || mode === 'full';
  const normalPaused = mode === 'paused' && !legal.requires_reauthorization;
  const flow = legal.flow ?? {};
  if (legal.requires_reauthorization
    || (!active && !normalPaused && (!flow.terms_event_id || !flow.risk_event_id || flow.stage === 'pre_mode'))) {
    return 'needs_terms';
  }
  if (mode === 'paused') return 'paused';
  if (mode !== 'full') return 'needs_full';
  if (consent.phase === 'permission_required' || consent.reload_required === true) return 'needs_site_access';
  if (pairing?.state === 'paired') return 'paired';
  if (pairing?.state === 'pairing' || pairing?.state === 'compare') return 'pairing';
  if (pairing?.state === 'setup_incomplete' || pairing?.state === 'unavailable') return 'needs_account';
  return 'ready_to_pair';
}

export function isDesktopMessage(message) {
  if (exactKeys(message, ['type', 'version', 'step'])) {
    return message.type === 'open' && message.version === DESKTOP_PORT_VERSION && DESKTOP_STEPS.includes(message.step);
  }
  return exactKeys(message, ['type', 'version'])
    && ['pair', 'cancel'].includes(message.type) && message.version === DESKTOP_PORT_VERSION;
}

function attemptProjection(announced) {
  if (announced?.state === 'pairing') return { state: 'pairing', comparison_code: null };
  if (announced?.state === 'compare' && /^\d{6}$/u.test(announced.comparison_code ?? '')) {
    return { state: 'compare', comparison_code: announced.comparison_code };
  }
  if (announced?.state === 'paired') return { state: 'paired', comparison_code: null };
  if (announced?.state === 'desktop_not_ready') return { state: 'not_ready', comparison_code: null };
  if (announced?.state === 'unpaired') return { state: 'cancelled', comparison_code: null };
  return { state: 'failed', comparison_code: null };
}

export function registerDesktopPort({
  chromeApi = globalThis.chrome,
  origin = LOCAL_SERVICE_ORIGIN,
  companion,
  readState,
  openStep,
  onPaired = async () => {},
  changeSources = [],
  now = Date.now,
} = {}) {
  if (!companion || typeof readState !== 'function' || typeof openStep !== 'function') {
    throw new TypeError('Desktop port dependencies are required');
  }
  const ports = new Set();
  let scheduled = false;

  const writeLink = () => Promise.resolve(chromeApi.storage?.session?.set?.({ [DESKTOP_LINK_STORAGE_KEY]: ports.size }))
    .catch(() => undefined);

  async function publish() {
    scheduled = false;
    if (ports.size === 0) return;
    let stage = 'unavailable';
    try { stage = desktopStage(await readState()); } catch {}
    for (const entry of ports) entry.push(stage);
  }
  // Coalesce bursts of change events into one read.
  function changed() {
    if (scheduled || ports.size === 0) return;
    scheduled = true;
    queueMicrotask(() => { void publish(); });
  }

  function connect(port) {
    if (port?.name !== DESKTOP_PORT_NAME) return;
    if (!admitDesktopSender(port.sender, origin)) { try { port.disconnect(); } catch {} return; }
    const owner = Object.freeze({ desktop: true });
    const controller = new AbortController();
    const anchorTab = Object.freeze({ id: port.sender.tab.id, windowId: port.sender.tab.windowId });
    let attempt = null, lastSent = '', lastOpenAt = -Infinity, closed = false;
    const entry = {
      push(stage) {
        if (closed) return;
        const message = { type: 'state', version: DESKTOP_PORT_VERSION, stage, attempt };
        const encoded = JSON.stringify(message);
        if (encoded === lastSent) return;
        lastSent = encoded;
        try { port.postMessage(message); } catch {}
      },
    };
    const unsubscribe = companion.subscribe((announced) => {
      if (typeof announced?.state === 'string' && companion.owns(owner)) {
        attempt = attemptProjection(announced);
      }
      changed();
    });
    ports.add(entry);
    void writeLink();
    port.onDisconnect.addListener(() => {
      closed = true;
      ports.delete(entry);
      unsubscribe();
      // Closing the desktop page cancels the attempt it started, like a setup page.
      controller.abort();
      companion.cancelFor(owner);
      void writeLink();
    });
    port.onMessage.addListener((message) => {
      if (!isDesktopMessage(message)) return;
      if (message.type === 'open') {
        const at = now();
        if (at - lastOpenAt < OPEN_INTERVAL_MS) return;
        lastOpenAt = at;
        void Promise.resolve(openStep(message.step, { anchorTab })).catch(() => undefined);
        return;
      }
      if (message.type === 'cancel') { companion.cancelFor(owner); return; }
      void (async () => {
        const started = await companion.pairFor(owner, { signal: controller.signal, onPaired });
        if (started && !closed) attempt = { state: 'paired', comparison_code: null };
      })().catch(async () => {
        if (closed) return;
        try { attempt = attemptProjection(await companion.status()); }
        catch { attempt = { state: 'failed', comparison_code: null }; }
      }).finally(() => { if (!closed) { lastSent = ''; changed(); } });
    });
    changed();
  }

  let registered = false;
  const unsubscribers = [];
  return Object.freeze({
    register() {
      if (registered) return;
      registered = true;
      // A worker restart drops every port, so a stored count is stale.
      void writeLink();
      chromeApi.runtime.onConnectExternal?.addListener(connect);
      for (const source of changeSources) unsubscribers.push(source(changed));
    },
    changed,
    get connected() { return ports.size; },
    unregister() {
      if (!registered) return;
      registered = false;
      chromeApi.runtime.onConnectExternal.removeListener?.(connect);
      for (const unsubscribe of unsubscribers.splice(0)) unsubscribe?.();
    },
  });
}
