import { LOCAL_SERVICE_ORIGIN } from '../transport/local-service-endpoints.mjs';

export const WORKSPACE_NAVIGATION_PORT = 'ofca.workspace.navigation.v1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));

/** The admitted document navigates itself; a reused tab is never a target. */
export function createWorkspaceNavigation({ workspace, runtime = globalThis.chrome?.runtime,
  location = globalThis.location, window = globalThis.window }) {
  let port = null, active = false, navigating = false;
  let retry = null, attempts = 0;
  const seen = new Set();
  const stop = () => { active = false; clearTimeout(retry); retry = null; const old = port; port = null; old?.disconnect(); };
  const start = () => {
    if (port || !runtime?.connect) return;
    active = true; navigating = false;
    let connected;
    try { connected = runtime.connect({ name: WORKSPACE_NAVIGATION_PORT }); } catch { return; }
    port = connected;
    connected.onDisconnect.addListener(() => {
      if (port !== connected) return; port = null;
      if (active && attempts < 3) retry = setTimeout(() => { retry = null; if (active) start(); }, [200, 500, 1000][attempts++]);
    });
    connected.onMessage.addListener((request) => {
      if (active && connected === port && exact(request, ['type']) && request.type === 'ready') { attempts = 0; return; }
      const recovery = request?.type === 'recover';
      if (!active || navigating || connected !== port || !exact(request, recovery
        ? ['type', 'request_id', 'journey_id', 'previous_journey_id', 'draft_scope', 'expected_url', 'route']
        : ['type', 'request_id', 'journey_id', 'draft_scope', 'expected_url', 'route'])
        || (!recovery && request.type !== 'navigate') || !UUID.test(request.request_id) || seen.has(request.request_id)
        || !UUID.test(request.journey_id) || (recovery && (!UUID.test(request.previous_journey_id) || request.route !== 'provisioning'))
        || !['provisioning', 'bridge'].includes(request.route)
        || !exact(request.draft_scope, ['scope_id', 'disclosure_bundle_id'])) return;
      const current = workspace();
      if (!current || current.journey_id !== (recovery ? request.previous_journey_id : request.journey_id)
        || current.draft_scope.scope_id !== request.draft_scope.scope_id
        || current.draft_scope.disclosure_bundle_id !== request.draft_scope.disclosure_bundle_id
        || location.href !== request.expected_url
        || location.href !== `${runtime.getURL('setup.html')}#journey=${current.journey_id}`) return;
      if (seen.size >= 64) return;
      seen.add(request.request_id); navigating = true;
      location.replace(`${LOCAL_SERVICE_ORIGIN}${request.route === 'bridge' ? '/' : '/provisioning'}#journey=${request.journey_id}`);
    });
  };
  window.addEventListener('pagehide', stop);
  window.addEventListener('pageshow', start);
  start();
  return { stop };
}
