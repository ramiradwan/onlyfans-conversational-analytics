import { inspectFrames } from './shift-watcher.mjs';
import { dynamicSequence } from './dynamic-matrix.mjs';
import { inspectShiftTimeline } from './shift-timeline.mjs';
import { failureSummary } from './failure-summary.mjs';

const known = new Set(dynamicSequence('settings', 390));
const contentChanges = new Map([
  ['loading', 'Initial component mount.'],
  ...['pairing.pins', 'history.get', 'vault.get', 'onboarding.readiness'].map(key => ['response:' + key, 'Initial API response reveals its compact section.']),
  ['archive:open', 'Explicit archive form expansion.'],
  ['archive:cancel', 'Explicit archive form closure.'],
  ...['selection', 'waiting', 'review', 'accepted', 'unavailable'].map(state => ['activation:' + state, 'Confirmed activation task changes its available decisions.']),
  ...['refusal', 'network', 'success'].map(outcome => ['redemption:' + outcome + ':open', 'Explicit activation-code form expansion.']),
  ['redemption:success:readiness', 'Confirmed activation completes and closes the code form.'],
  ['pairing:open', 'Explicit manual comparison task expansion.'],
  ['pairing:pending', 'Accepted manual connection window presents its waiting task.'],
  ['pairing:expired', 'Expired manual comparison task removes its decisions.'],
  ...['declined', 'cancelled', 'confirmed', 'refused'].flatMap(outcome => [
    ['pairing:' + outcome + ':start', 'Explicit manual comparison task expansion after isolated fixture reset.'],
    ['pairing:' + outcome + ':comparison', 'Confirmed comparison task presents its code and decisions.'],
    ...(['declined', 'cancelled', 'confirmed'].includes(outcome) ? [['pairing:' + outcome + ':result', 'Confirmed manual comparison outcome closes its task.']] : []),
  ]),
]);

// Auxiliary windows only surround these existing driver operations. They do not
// extend a tested action's window or change its 268-observation inventory.
const fixtureContentChanges = new Map([
  ['fixture:response-order-reset', 'Synthetic API identity reset before one response permutation.'],
  ['fixture:response-error-reset', 'Synthetic API identity reset before one failed response.'],
  ['fixture:response-error-release', 'Initial responses for the other reset API objects.'],
  ['fixture:response-final-reset', 'Restore all initial API responses after permutations.'],
  ...['vault.get', 'onboarding.readiness', 'pairing.pins'].map(key => ['fixture:refresh:' + key, 'Existing synthetic API identity replacement and initial response: ' + key + '.']),
  ['fixture:reset-activation-task', 'Remove the previous explicit activation task before manual-code cases.'],
  ['fixture:activation-code-close', 'Explicit closure of the manual activation-code form.'],
  ['fixture:pairing-outcome-reset', 'Existing Settings remount and initial responses before an independent comparison outcome.'],
  ['fixture:pairing-open-result', 'Accepted manual connection window reveals its waiting task.'],
  ['fixture:pairing-pins-release', 'Confirmed comparison result refreshes the available connection controls.'],
]);
const fixtureStable = new Set([
  'fixture:initial-connection', 'fixture:before-browser-controls', 'fixture:before-history', 'fixture:after-history',
  'fixture:activation-admission-setup', 'fixture:after-admission', 'fixture:activation-code-input',
  'fixture:pairing-companion-notice', 'fixture:pairing-codes-match', 'fixture:scroll-audit',
]);

// Emit only source-owned stage IDs and fixed category counts. Raw browser
// failures, region names, geometry and rendered content stay in local evidence.
export function settingsFailureSummary(observerErrors) {
  let remaining = [...observerErrors];
  const stages = [];
  for (const id of [...known, ...fixtureContentChanges.keys(), ...fixtureStable]) {
    const matches = error => typeof error === 'string' && error.startsWith(id + ': ');
    const errors = remaining.filter(matches);
    if (!errors.length) continue;
    stages.push({ id, counts: failureSummary([], errors) });
    remaining = remaining.filter(error => !matches(error));
  }
  return { stages, unassigned: failureSummary([], remaining) };
}

// Locate an unassigned shift using only closed stage names. Exact timestamps,
// layout sources and geometry remain in the private capture, never CI output.
export function settingsUnassignedShiftSummary(attribution, transitions, auxiliary = []) {
  const windows = [...transitions.map(window => ({ ...window, kind: 'transition' })),
    ...auxiliary.map(window => ({ ...window, kind: 'fixture' }))]
    .filter(window => Number.isFinite(window.before?.nativeAt) && Number.isFinite(window.after?.nativeAt))
    .sort((a, b) => a.before.nativeAt - b.before.nativeAt);
  const label = window => window && (window.kind === 'transition' ? known.has(window.id)
    : fixtureContentChanges.has(window.id) || fixtureStable.has(window.id)) ? window.id : null;
  const groups = new Map();
  for (const entry of attribution ?? []) {
    if (entry.classification !== 'unattributed') continue;
    const at = entry.at;
    const previous = windows.findLast(window => window.after.nativeAt <= at);
    const next = windows.find(window => window.before.nativeAt >= at);
    const owners = windows.filter(window => at > window.before.nativeAt && at < window.after.nativeAt);
    const boundary = windows.some(window => at === window.before.nativeAt || at === window.after.nativeAt);
    const position = !Number.isFinite(at) || !windows.length ? 'unknown' : boundary ? 'boundary'
      : at < windows[0].before.nativeAt ? 'startup' : at > windows.at(-1).after.nativeAt ? 'tail'
        : owners.length > 1 ? 'overlap' : 'gap';
    const context = { position, after: label(previous), before: label(next) };
    const key = JSON.stringify(context);
    const group = groups.get(key) ?? { ...context, count: 0 };
    group.count++;
    groups.set(key, group);
  }
  return [...groups.values()];
}

// Ordinary screenshots use the same task policy as the causal Settings matrix.
// They must not treat initial owner responses or an explicit form opening as a
// passive status update, or extend those windows into subsequent idle frames.
export function ordinarySettingsTransition(variant) {
  const transitions = {
    'activation-review': 'activation:review', 'activation-waiting': 'activation:waiting',
    'activation-error': 'activation:review', 'activation-provider-selection': 'activation:selection',
    'pairing-comparison': 'pairing:open', 'history-consent': 'pairing:confirmed:result',
    'archive-dialog': 'archive:open', 'delete-confirmation': 'control:authoritative',
    'linked-reconnecting': 'control:authoritative',
  };
  if (!Object.hasOwn(transitions, variant)) throw new Error('Unclassified ordinary Settings action: ' + variant);
  return transitions[variant];
}

export function settingsFixturePolicy(id) {
  const reason = fixtureContentChanges.get(id);
  if (reason) return { geometry: 'content-transition', reason };
  if (fixtureStable.has(id)) return { geometry: 'stable-status' };
  throw new Error('Unclassified Settings auxiliary window: ' + id);
}

export function settingsGeometryPolicy(id) {
  if (!known.has(id)) throw new Error('Unclassified Settings transition: ' + id);
  if (id.startsWith('fixture-api:')) return { geometry: 'content-transition', reason: 'Synthetic fixture replaces all API object identities and reloads their sections.' };
  const reason = contentChanges.get(id);
  return reason ? { geometry: 'content-transition', reason } : { geometry: 'stable-status' };
}

/** Task expansions may reflow; every frame still retains all persistent sections
 * and readable content. Same-owner status changes retain exact geometry. */
export function inspectSettingsFrames(frames, transitions, requiredRegions, { rawShifts, nativeTimeline, auxiliary = [] } = {}) {
  const failures = new Set();
  if (!frames.length) failures.add('No Settings frames captured');
  if (!transitions.length) failures.add('No Settings transitions captured');
  for (const frame of frames) {
    for (const error of frame.errors ?? []) failures.add(error);
    const present = new Set(frame.regions.map(region => region.id));
    for (const id of requiredRegions) if (!present.has(id)) failures.add(`${id}: missing from captured frame`);
    for (const region of frame.regions) if (region.overflow) failures.add(`${region.id}: overflow`);
  }
  const checkGeometry = (transition, policy) => {
    if (!Number.isInteger(transition.before.frame) || !Number.isInteger(transition.after.frame)
      || transition.before.frame < 0 || transition.after.frame < transition.before.frame || transition.after.frame > frames.length) {
      failures.add(`${transition.id}: invalid geometry frame interval`);
    }
    const measured = frames.slice(Math.max(0, transition.before.frame - 1), transition.after.frame);
    if (measured.length === 0) failures.add(`${transition.id}: no status frame`);
    if (policy.geometry === 'stable-status') {
      // Geometry and shift occurrence are separate checks. The complete raw
      // ledger below grades every shift, including those delivered much later.
      const current = measured.map(frame => ({ ...frame, shifts: [] }));
      for (const failure of inspectFrames(current)) failures.add(`${transition.id}: ${failure}`);
    }
    return { id: transition.id, ...policy };
  };
  const policies = transitions.map(transition => checkGeometry(transition, settingsGeometryPolicy(transition.id)));
  const auxiliaryPolicies = auxiliary.map(transition => checkGeometry(transition, settingsFixturePolicy(transition.id)));
  const windows = transitions.map((transition, index) => ({ ...transition, ...policies[index], kind: 'transition', index }));
  windows.push(...auxiliary.map((transition, index) => ({ ...transition, ...auxiliaryPolicies[index], kind: 'fixture', index })));
  const timeline = inspectShiftTimeline(frames, windows, rawShifts, nativeTimeline);
  for (const failure of timeline.failures) failures.add(failure);
  return { failures: [...failures], policies, auxiliaryPolicies, shiftAttribution: timeline.attribution };
}
