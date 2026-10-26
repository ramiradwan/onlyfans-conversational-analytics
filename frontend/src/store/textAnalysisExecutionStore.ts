import { createStore } from 'zustand';

import type { BridgeTransportState } from './transportStore';
import { parseEnrollmentView, textAnalysisCanonicalJson, type ClientIntentCursor, type EnrollmentCommand, type EnrollmentView } from '../analytics/textAnalysisContract';
import { textAnalysisApi } from '../services/textAnalysisApi';
import { createTextAnalysisClient, type TextAnalysisClient, type TextAnalysisLifetime, type TextAnalysisObservation } from '../services/textAnalysisClient';

export type TextAnalysisContext = Pick<BridgeTransportState, 'creatorAccountId' | 'session' | 'connection'>;
export type TextAnalysisRole = 'creator-ceo' | 'operator' | null;
export function textAnalysisScopeKey(context: TextAnalysisContext, role: TextAnalysisRole, account: string | null) {
  return JSON.stringify([account, context.creatorAccountId, role, context.connection, context.session?.connection_id, context.session?.bridge_session_id]);
}
export function textAnalysisScopeReady(context: TextAnalysisContext, role: TextAnalysisRole, account: string | null) {
  return Boolean(account && context.creatorAccountId === account && context.session?.creator_account_id === account
    && context.connection === 'connected' && role === 'creator-ceo');
}
export interface TextAnalysisExecutionState {
  binding: string;
  status: 'unavailable' | 'loading' | 'ready' | 'error';
  view: EnrollmentView | null;
  busy: boolean;
  message: string | null;
  observations: TextAnalysisObservation[];
  nextAfter: ClientIntentCursor | null;
  recoveryOverflow: boolean;
  recoveryConflict: boolean;
}
const initial: TextAnalysisExecutionState = { binding: '', status: 'unavailable', view: null, busy: false,
  message: 'Connect with an authorized creator session to manage processing.', observations: [], nextAfter: null, recoveryOverflow: false, recoveryConflict: false };
export function createTextAnalysisExecutionStore(client: TextAnalysisClient = createTextAnalysisClient(textAnalysisApi)) {
  const store = createStore<TextAnalysisExecutionState>(() => ({ ...initial }));
  let key = '', account: string | null = null, enabled = false, sequence = 0;
  let controller: AbortController | null = null;
  function invalidate() { sequence += 1; controller?.abort(); controller = null; }
  function begin() {
    invalidate(); controller = new AbortController(); const id = sequence, binding = key, signal = controller.signal;
    const active = () => enabled && id === sequence && binding === key && !signal.aborted;
    return { active, signal } satisfies TextAnalysisLifetime;
  }
  function setObservation(value: TextAnalysisObservation) {
    const prior = store.getState().observations.find((item) => item.locator.kind === value.locator.kind && item.locator.key === value.locator.key);
    value = reconcile(prior, value);
    const rows = store.getState().observations.filter((item) => item.locator.kind !== value.locator.kind || item.locator.key !== value.locator.key);
    if (rows.length >= 4096) { store.setState({ recoveryOverflow: true }); return null; }
    store.setState({ observations: [value, ...rows] });
    return value;
  }
  function reconcile(prior: TextAnalysisObservation | undefined, value: TextAnalysisObservation): TextAnalysisObservation {
    if (prior?.intent && value.intent && textAnalysisCanonicalJson(prior.intent) !== textAnalysisCanonicalJson(value.intent)) {
      store.setState({ recoveryConflict: true }); return prior;
    }
    if (prior?.intent && value.intent === null) return { ...value, intent: prior.intent };
    return value;
  }
  async function refresh(after?: ClientIntentCursor) {
    if (!enabled || store.getState().busy) return;
    const lifetime = begin(); store.setState({ busy: true, status: 'loading', view: null, message: null });
    const results = await Promise.allSettled([client.api.current(lifetime.signal), client.restore(after, lifetime)]);
    if (!lifetime.active()) return;
    const [current, recovered] = results;
    let view: EnrollmentView | null = null;
    if (current.status === 'fulfilled') {
      try { view = parseEnrollmentView(current.value, account!); } catch { /* Refuse a mismatched view. */ }
    }
    store.setState({ view, status: view ? 'ready' : 'error', busy: false,
      observations: recovered.status === 'fulfilled' ? mergeObservations(recovered.value.items.filter((item) => item.locator.kind === 'enrollment_command')) : store.getState().observations,
      nextAfter: recovered.status === 'fulfilled' ? recovered.value.next_after : store.getState().nextAfter,
      message: !view ? 'Processing management is unavailable for this session. Saved results have separate read access.'
        : recovered.status === 'rejected' ? 'Retained requests could not be checked. Check status before changing processing.' : null });
  }
  function mergeObservations(items: TextAnalysisObservation[]) {
    const fresh = new Set(items.map((item) => `${item.locator.kind}:${item.locator.key}`));
    const previous = store.getState().observations;
    const merged = [...previous.filter((item) => item.state !== 'resolved' && !fresh.has(`${item.locator.kind}:${item.locator.key}`)),
      ...items.map((item) => reconcile(previous.find((prior) => prior.locator.kind === item.locator.kind && prior.locator.key === item.locator.key), item))];
    if (merged.length > 4096) { store.setState({ recoveryOverflow: true }); return store.getState().observations; }
    return merged;
  }
  function setContext(context: TextAnalysisContext, role: TextAnalysisRole, expectedAccount: string | null) {
    const next = textAnalysisScopeKey(context, role, expectedAccount); if (next === key) return;
    invalidate(); key = next; account = expectedAccount; enabled = textAnalysisScopeReady(context, role, expectedAccount);
    store.setState({ ...initial, binding: key }); if (enabled) void refresh();
  }
  function buildCommand(action: EnrollmentCommand['action'], profile?: string): EnrollmentCommand | null {
    const state = store.getState(), current = state.view?.current;
    if (!enabled || state.busy || !current || state.message || state.nextAfter || state.recoveryOverflow || state.recoveryConflict || state.observations.some((item) => item.state !== 'resolved')) return null;
    if ((action === 'enable') !== (current.enrollment_id === null)) return null;
    if (['enable', 'replace'].includes(action) && !profile) return null;
    if (action === 'resume' && current.state !== 'paused' || action === 'pause' && current.state !== 'enabled'
      || action === 'retire' && current.state === 'retired') return null;
    return { schema_version: 'text-analysis-enrollment-command.v1', command_key: crypto.randomUUID(),
      expected_enrollment_id: current.enrollment_id, expected_mode_version: current.mode_version, action,
      expected_profile_sha256: action === 'enable' || action === 'replace' ? profile ?? null : null };
  }
  async function observe(work: (lifetime: TextAnalysisLifetime) => Promise<TextAnalysisObservation>) {
    if (!enabled || store.getState().busy) return;
    const lifetime = begin(); store.setState({ busy: true, message: null });
    try {
      const incoming = await work(lifetime); if (!lifetime.active()) return;
      const value = setObservation(incoming); if (value === null || store.getState().recoveryConflict) return;
      if (value.state === 'resolved') {
        store.setState({ view: null });
        const view = parseEnrollmentView(await client.api.current(lifetime.signal), account!); if (!lifetime.active()) return;
        store.setState({ view, status: 'ready' });
      }
      if (lifetime.active()) store.setState({ message: value.state === 'resolved' ? null
        : value.state === 'absent' ? 'The retained command has no recorded outcome. You may explicitly retry that exact command.'
          : 'The command outcome is unknown. Check the original retained request before continuing.' });
    } catch {
      if (lifetime.active()) store.setState({ view: null, status: 'error', message: 'The command could not be verified. Check its original status before continuing.' });
    } finally { if (lifetime.active()) store.setState({ busy: false }); }
  }
  function submit(command: EnrollmentCommand) {
    const state = store.getState();
    if (state.message || state.nextAfter || state.recoveryOverflow || state.recoveryConflict || state.observations.some((item) => item.state !== 'resolved')) return Promise.resolve();
    if (!state.view || command.expected_enrollment_id !== state.view.current.enrollment_id || command.expected_mode_version !== state.view.current.mode_version) return Promise.resolve();
    return observe((lifetime) => client.command(command, lifetime));
  }
  function recover(locator: ClientIntentCursor) {
    return locator.kind === 'enrollment_command' ? observe((lifetime) => client.read(locator, lifetime)) : Promise.resolve();
  }
  function retry(observation: TextAnalysisObservation) {
    if (observation.state === 'resolved' || !observation.intent || observation.intent.kind !== 'enrollment_command') return Promise.resolve();
    const intent = observation.intent;
    return observe((lifetime) => client.retry(intent, lifetime));
  }
  function dispose() { invalidate(); key = ''; account = null; enabled = false; store.setState({ ...initial }); }
  return { ...store, actions: { setContext, refresh, buildCommand, submit, recover, retry, dispose } };
}
export type TextAnalysisExecutionStore = ReturnType<typeof createTextAnalysisExecutionStore>;
