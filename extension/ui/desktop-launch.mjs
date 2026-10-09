import { workspaceAppLink } from '../runtime/onboarding-native-launch.mjs';

const identity = (record) => record && JSON.stringify([record.journey_id,
  record.draft_scope?.scope_id, record.draft_scope?.disclosure_bundle_id]);
export const authenticatedDesktop = (model) => model?.desktopRuntimeReachable === true
  && model.status?.brain_reachable === true && model.status.delivery?.transport_state === 'authenticated';

/** Presentation of one explicit launch. The worker separately owns return intent. */
export function createDesktopLaunch({ workspace, prepare, dispatch, navigate, changed,
  setTimer = setTimeout, clearTimer = clearTimeout, fallbackMs = 12_000 }) {
  let state = 'idle', scope = null, generation = 0, timer = null;
  const publish = (next) => { state = next; changed(); };
  const cancelTimer = () => { if (timer !== null) clearTimer(timer); timer = null; };
  const current = (value) => value === generation && scope === identity(workspace());
  async function continueToApp(value) {
    if (!current(value) || state === 'continuing') return;
    cancelTimer(); publish('continuing');
    try { await navigate(); }
    catch { if (current(value)) publish('unconfirmed'); }
  }
  return Object.freeze({
    get state() { return state; },
    async launch(model) {
      if (['opening', 'continuing'].includes(state)) return;
      const record = workspace();
      if (!record) return;
      scope = identity(record);
      const value = ++generation;
      if (authenticatedDesktop(model)) { await continueToApp(value); return; }
      publish('opening');
      try {
        const prepared = await prepare();
        if (!current(value)) return;
        if (!prepared || Object.keys(prepared).length !== 2 || prepared.journey_id !== record.journey_id
          || prepared.app_link !== workspaceAppLink(record.journey_id)) throw Error('Invalid app link');
        // Called only by the button handler. No visibility event, retry timer,
        // status change or page restoration is allowed to dispatch an app link.
        dispatch(prepared.app_link);
        timer = setTimer(() => { timer = null; if (current(value) && state === 'opening') publish('unconfirmed'); }, fallbackMs);
      } catch { if (current(value)) publish('unconfirmed'); }
    },
    observe(model) {
      if (scope !== null && scope !== identity(workspace())) {
        generation++; cancelTimer(); scope = null; state = 'idle';
      }
      if (['opening', 'unconfirmed'].includes(state) && authenticatedDesktop(model)) void continueToApp(generation);
    },
    stop() { generation++; cancelTimer(); scope = null; state = 'idle'; },
  });
}

export function desktopLaunchJourney(journey, state) {
  if (!['desktop_app_needed', 'desktop_app_unavailable'].includes(journey.id)) return journey;
  if (state === 'opening' || state === 'continuing') return { ...journey, tone: 'progress',
    title: state === 'opening' ? 'Waiting for the desktop app…' : 'Opening setup…',
    body: '', primaryAction: null, primaryLabel: null };
  if (state === 'unconfirmed') return { ...journey, tone: 'warning', title: 'Open the desktop app',
    body: 'Open the app from the Start menu.',
    primaryAction: 'open_desktop', primaryLabel: 'Open desktop app' };
  return journey;
}
