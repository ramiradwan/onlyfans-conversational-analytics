const date = '2026-06-30T10:00:00.000Z';
const states = [
  ['never_checked', null], ['current', null],
  ...['canary', 'catch_up', 'first_check'].map((reason) => ['checking', reason]),
  ...['awaiting_check', 'daily_cap', 'check_incomplete', 'not_observing'].map((reason) => ['behind', reason]),
  ...['user_paused', 'consent_needed', 'extension_offline', 'no_onlyfans_tab', 'onlyfans_sleeping', 'account_changed', 'applying_settings', 'capture_off', 'extension_outdated', 'unrecognized-reason'].map((reason) => ['paused', reason]),
];
const frames = states.map(([status, reason]) => ({ freshness: { status, reason, uncertain_since: status === 'behind' ? '2025-12-31T21:59:00.000Z' : null, last_closed_at: status === 'never_checked' ? null : date, observing_since: date }, bridge: 'connected', snapshotUsable: true }));
frames.push({ ...frames[2], freshness: { ...frames[2].freshness, last_closed_at: null } });
for (const bridge of ['connecting', 'handshaking', 'disconnected', 'reconnecting', 'error']) frames.push({ ...frames[1], bridge });
frames.push({ ...frames[1], snapshotUsable: false });

export const FRESHNESS_VERTICES = frames.map((frame, index) => ({ ...frame, id: 'freshness-' + index }));
const matrixVertices = FRESHNESS_VERTICES.slice(0, states.length);
export const FRESHNESS_TRANSITIONS = matrixVertices.flatMap((from) => matrixVertices.filter((to) => to.id !== from.id).map((to) => ({ from, to })));

export function freshnessCases() {
  return [390, 1440].flatMap(width => ['light', 'dark'].flatMap(mode => [1, 1.25].flatMap(fontScale => ['reduce', 'no-preference'].map(motion => ({ width, mode, fontScale, motion })))));
}
