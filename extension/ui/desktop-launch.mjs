import { workspaceAppLink } from '../runtime/onboarding-native-launch.mjs';

const identity = (record) => record && JSON.stringify([record.journey_id,
  record.draft_scope?.scope_id, record.draft_scope?.disclosure_bundle_id]);
/** Dispatch is an intent. Only the scoped browser-task acknowledgement confirms adoption. */
export function createDesktopLaunch({ workspace, prepare, dispatch, changed,
  setTimer = setTimeout, clearTimer = clearTimeout, fallbackMs = 12_000 }) {
  let state = 'idle', scope = null, generation = 0, timer = null, adopted = false;
  const publish = (next) => { state = next; changed(); };
  const cancelTimer = () => { if (timer !== null) clearTimer(timer); timer = null; };
  const current = (value) => value === generation && scope === identity(workspace());
  return Object.freeze({
    get state() { return state; },
    async launch() {
      if (state === 'opening') return;
      const record = workspace();
      if (!record) return;
      scope = identity(record);
      const value = ++generation;
      cancelTimer(); publish('opening');
      try {
        const prepared = await prepare();
        if (!current(value)) return;
        if (!prepared || Object.keys(prepared).length !== 2 || prepared.journey_id !== record.journey_id
          || prepared.app_link !== workspaceAppLink(record.journey_id)) throw Error('Invalid app link');
        // Called only by the button handler. No visibility event, retry timer,
        // status change or page restoration is allowed to dispatch an app link.
        dispatch(prepared.app_link);
        if (adopted) { publish('adopted'); return; }
        timer = setTimer(() => { timer = null; if (current(value) && state === 'opening') publish('unconfirmed'); }, fallbackMs);
      } catch { if (current(value)) publish('unconfirmed'); }
    },
    observe(acknowledged) {
      if (scope !== null && scope !== identity(workspace())) {
        generation++; cancelTimer(); scope = null; state = 'idle';
      }
      adopted = acknowledged === true;
      if (adopted && state !== 'adopted') { cancelTimer(); state = 'adopted'; }
      if (!adopted && state === 'adopted') state = 'idle';
    },
    stop() { generation++; cancelTimer(); scope = null; state = 'idle'; adopted = false; },
  });
}

export function desktopLaunchPresentation(state) {
  if (state === 'opening') return { title: 'Waiting for the desktop app…', body: 'Continue in the desktop window.' };
  if (state === 'unconfirmed') return { title: 'Desktop connection not confirmed', body: '' };
  if (state === 'adopted') return { title: 'Continue in the desktop app', body: 'Desktop setup has received this browser’s setup request.' };
  return { title: 'Full analytics uses the desktop app', body: 'Open Conversation Analytics on this computer to continue setup.' };
}
