// Browser-test adapter only. This file is never included in extension artifacts.
export function installSurfaceFixture(input) {
  const state = structuredClone(input), calls = [], ports = new Set();
  const event = () => { const listeners = new Set(); return { addListener: (fn) => listeners.add(fn),
    removeListener: (fn) => listeners.delete(fn), emit: (value) => { for (const fn of listeners) fn(value); } }; };
  const pairState = () => ({ state: state.pairing ?? (state.paired ? 'paired' : 'unpaired'),
    comparison_code: state.pairing === 'compare' && state.surface === 'setup' ? '483217' : null,
    owns_attempt: state.surface === 'setup' && ['pairing', 'compare'].includes(state.pairing) });
  const status = () => ({ consent: { mode: state.mode, resume_mode: state.resume ?? null, consent_epoch: 'fixture-epoch' },
    phase: state.phase ?? (state.mode === 'full' ? (state.paired ? 'full' : 'identity') : state.mode),
    reload_required: state.reload === true, onlyfans_permission: state.phase !== 'permission_required', history_permission: false,
    preview: { message_observations: 128, chat_observations: 24, inbound_observations: 80, outbound_observations: 48 },
    delivery: { transport_state: state.paired ? 'authenticated' : 'disconnected', pending_entries: 0, capture_drop_counts: {} } });
  const legal = () => ({ configured: state.configured !== false, consent_mode: state.mode,
    requires_reauthorization: state.reauthorization === true,
    bindings: { public_origin: 'https://legal.example.test', instruments: {
      terms_of_service: { public_url: '/terms' }, risk_disclosure: { public_url: '/risk' }, extension_privacy_notice: { public_url: '/privacy' } } },
    flow: { terms_event_id: !state.agreement || state.accepted || state.terms ? 'fixture-terms' : null,
      risk_event_id: !state.agreement || state.accepted || state.risk ? 'fixture-risk' : null,
      stage: state.agreement ? 'pre_mode' : state.choice ? 'mode_selection' : 'complete' } });
  window.__surfaceFixture = { state, calls, change(next) { Object.assign(state, next); for (const port of ports) port.onMessage.emit(pairState()); } };
  window.chrome = {
    runtime: { id: 'fixture', getURL: (name) => `${location.origin}/${name}`, getManifest: () => ({ version: '2.0.3' }),
      async sendMessage(message) {
        calls.push(structuredClone(message));
        if (state.runtimeUnavailable) throw new Error('fixture_runtime_unavailable');
        if (message.type === 'ofca.ui.status') return { ok: true, status: status() };
        if (message.type === 'ofca.legal-activation.status') return { ok: true, result: legal() };
        if (message.type === 'ofca.legal-activation.accept-terms') state.terms = true;
        else if (message.type === 'ofca.legal-activation.acknowledge-risk') state.risk = true;
        else if (message.type === 'ofca.legal-activation.activate-software') { state.agreement = false; state.choice = true; }
        else if (message.type === 'ofca.legal-activation.choose-mode') { state.mode = message.mode; state.choice = false; }
        else if (message.type === 'ofca.ui.transition') { state.resume = state.mode; state.mode = message.mode === 'resume' ? input.mode : message.mode; }
        else if (message.type === 'ofca.ui.reload-tabs') state.reload = false;
        return { ok: true, status: status() };
      },
      connect() {
        const port = { onMessage: event(), onDisconnect: event(),
          postMessage(message) {
            if (message.type === 'readiness') {
              if (!state.checking) queueMicrotask(() => port.onMessage.emit({ type: 'analysis_readiness',
                commercial_authority: state.commercial ?? 'active', analysis_admission: state.ready ? 'admitted' : 'blocked' }));
              return;
            }
            if (message.type === 'pair') { calls.push(message); state.pairing = 'compare'; }
            if (message.type === 'cancel') { calls.push(message); state.pairing = 'unpaired'; }
            if (message.type === 'forget') { state.paired = false; state.pairing = 'unpaired';
              queueMicrotask(() => port.onMessage.emit({ type: 'pairing_command_result', command: 'forget', ok: true })); }
            queueMicrotask(() => port.onMessage.emit(pairState()));
          },
          disconnect() { ports.delete(port); port.onDisconnect.emit(); },
        }; ports.add(port); queueMicrotask(() => port.onMessage.emit(pairState())); return port;
      } },
    permissions: { request: async (request) => { calls.push({ type: 'permission', request, userGesture: navigator.userActivation.isActive }); return state.permissionGranted !== false; } },
    tabs: { create: async (value) => { calls.push({ type: 'tab', ...value }); return { id: 2 }; } },
    storage: { onChanged: event() },
  };
  const originalFetch = window.fetch;
  window.fetch = (url, options) => String(url).endsWith('/extension-config.json')
    ? Promise.resolve({ json: async () => ({ schema: 'ofca-extension-config/v1', dashboard_url: 'http://bridge.localhost:17871/', history_settings_url: 'http://bridge.localhost:17871/settings' }) })
    : originalFetch(url, options);
  window.WebSocket = class { constructor() { queueMicrotask(() => state.reachable ? this.onopen?.() : this.onerror?.()); } close() {} };
}
