import { createStore } from 'zustand';

import { textAnalysisScopeKey, textAnalysisScopeReady, type TextAnalysisContext, type TextAnalysisRole } from './textAnalysisExecutionStore';
import { expectedAccountRef, evidenceKey, parseSourceEvidence, type QuestionEvidence, type SourceEvidence } from '../analytics/questionContract';
import { parsePreviewOutcome, textAnalysisCanonicalJson, type ClientIntentCursor, type PreviewOutcome, type SavedPreview } from '../analytics/textAnalysisContract';
import { textAnalysisApi } from '../services/textAnalysisApi';
import { createTextAnalysisClient, type TextAnalysisClient, type TextAnalysisLifetime, type TextAnalysisObservation } from '../services/textAnalysisClient';

export interface TextAnalysisPreviewState {
  binding: string;
  busy: boolean;
  message: string | null;
  outcome: PreviewOutcome | null;
  saved: SavedPreview | null;
  savedIds: string[];
  nextSaved: string | null;
  observations: TextAnalysisObservation[];
  nextIntent: ClientIntentCursor | null;
  recoveryOverflow: boolean;
  recoveryConflict: boolean;
  source: SourceEvidence | null;
  sourceLoading: boolean;
  sourceError: string | null;
}
const initial: TextAnalysisPreviewState = { binding: '', busy: false, message: 'Connect with an authorized creator session to view previews.',
  outcome: null, saved: null, savedIds: [], nextSaved: null, observations: [], nextIntent: null, recoveryOverflow: false, recoveryConflict: false,
  source: null, sourceLoading: false, sourceError: null };
function nonterminal(value: TextAnalysisObservation) {
  return (value.state === 'resolved' || value.state === 'confirmable') && value.intent.kind !== 'enrollment_command'
    && 'request' in value.result && ['prepared', 'pending', 'running'].includes(value.result.receipt.state);
}
export function createTextAnalysisPreviewStore(client: TextAnalysisClient = createTextAnalysisClient(textAnalysisApi)) {
  const store = createStore<TextAnalysisPreviewState>(() => ({ ...initial }));
  let key = '', account: string | null = null, enabled = false, sequence = 0;
  let controller: AbortController | null = null;
  const invalidate = () => { sequence += 1; controller?.abort(); controller = null; };
  function begin() {
    invalidate(); controller = new AbortController(); const id = sequence, binding = key, signal = controller.signal;
    return { signal, active: () => enabled && id === sequence && binding === key && !signal.aborted } satisfies TextAnalysisLifetime;
  }
  function remember(value: TextAnalysisObservation) {
    const prior = store.getState().observations.find((item) => item.locator.kind === value.locator.kind && item.locator.key === value.locator.key);
    value = reconcile(prior, value);
    const items = store.getState().observations.filter((item) => item.locator.kind !== value.locator.kind || item.locator.key !== value.locator.key);
    if (items.length >= 4096) { store.setState({ recoveryOverflow: true }); return null; }
    store.setState({ observations: [value, ...items] });
    return value;
  }
  function reconcile(prior: TextAnalysisObservation | undefined, value: TextAnalysisObservation): TextAnalysisObservation {
    if (prior?.intent && value.intent && textAnalysisCanonicalJson(prior.intent) !== textAnalysisCanonicalJson(value.intent)) {
      store.setState({ recoveryConflict: true }); return prior;
    }
    if (prior?.intent && value.intent === null) return { ...value, intent: prior.intent };
    return value;
  }
  async function refresh(afterIntent?: ClientIntentCursor, afterSaved?: string) {
    if (!enabled || store.getState().busy) return;
    const lifetime = begin(); store.setState({ busy: true, message: null, saved: null, source: null, sourceError: null, sourceLoading: false });
    const [intents, saved] = await Promise.allSettled([client.restore(afterIntent, lifetime), client.api.savedList(afterSaved, lifetime.signal)]);
    if (!lifetime.active()) return;
    const items = intents.status === 'fulfilled' ? intents.value.items.filter((item) => item.locator.kind !== 'enrollment_command') : [];
    const observations = intents.status === 'fulfilled' ? mergeObservations(items) : store.getState().observations;
    store.setState({ busy: false, observations, outcome: refreshedOutcome(items),
      nextIntent: intents.status === 'fulfilled' ? intents.value.next_after : store.getState().nextIntent,
      savedIds: saved.status === 'fulfilled' ? saved.value.items.map((item) => item.contribution_id) : [],
      nextSaved: saved.status === 'fulfilled' ? saved.value.next_after : null,
      message: intents.status === 'rejected' || saved.status === 'rejected' ? 'Some retained previews could not be checked. Check status before preparing another preview.' : null });
  }
  function refreshedOutcome(items: TextAnalysisObservation[]) {
    let outcome = store.getState().outcome;
    if (outcome === null || store.getState().recoveryConflict) return outcome;
    for (const item of items) {
      if ((item.state !== 'resolved' && item.state !== 'confirmable') || item.intent.kind === 'enrollment_command'
        || !('request' in item.result) || item.result.request.external_key !== outcome.request.external_key) continue;
      const next = item.result;
      if (next.receipt.job_id !== outcome.receipt.job_id || next.receipt.contribution_id !== outcome.receipt.contribution_id
        || textAnalysisCanonicalJson(next.request) !== textAnalysisCanonicalJson(outcome.request)
        || !['prepared', 'pending', 'running'].includes(outcome.receipt.state) && ['prepared', 'pending', 'running'].includes(next.receipt.state)) {
        store.setState({ recoveryConflict: true }); return outcome;
      }
      outcome = next;
    }
    return outcome;
  }
  function mergeObservations(items: TextAnalysisObservation[]) {
    const fresh = new Set(items.map((item) => `${item.locator.kind}:${item.locator.key}`));
    const previous = store.getState().observations;
    const merged = [...previous.filter((item) => (item.state !== 'resolved' || nonterminal(item)) && !fresh.has(`${item.locator.kind}:${item.locator.key}`)),
      ...items.map((item) => reconcile(previous.find((prior) => prior.locator.kind === item.locator.kind && prior.locator.key === item.locator.key), item))];
    if (merged.length > 4096) { store.setState({ recoveryOverflow: true }); return store.getState().observations; }
    return merged;
  }
  function setContext(context: TextAnalysisContext, role: TextAnalysisRole, expectedAccount: string | null) {
    const next = textAnalysisScopeKey(context, role, expectedAccount); if (next === key) return;
    invalidate(); key = next; account = expectedAccount; enabled = textAnalysisScopeReady(context, role, expectedAccount);
    store.setState({ ...initial, binding: key }); if (enabled) void refresh();
  }
  async function observe(work: (lifetime: TextAnalysisLifetime) => Promise<TextAnalysisObservation>) {
    if (!enabled || store.getState().busy) return;
    const lifetime = begin(); store.setState({ busy: true, message: null, saved: null, source: null, sourceLoading: false, sourceError: null });
    try {
      const incoming = await work(lifetime); if (!lifetime.active()) return;
      const value = remember(incoming); if (value === null || store.getState().recoveryConflict) return;
      if ((value.state === 'resolved' || value.state === 'confirmable') && value.intent.kind !== 'enrollment_command') {
        const outcome = parsePreviewOutcome(value.result, value.intent.key);
        store.setState({ outcome });
        if (outcome.receipt.state === 'committed') {
          const output = await client.api.saved(outcome.receipt.contribution_id, lifetime.signal);
          const expected = await expectedAccountRef(account!); if (!lifetime.active()) return;
          if (!output.output.summaries.every((summary) => summary.evidence.every((item) => item.reference.account_ref === expected))) throw new Error('Account changed');
          store.setState({ saved: output });
        }
      }
      if (lifetime.active()) store.setState({ message: value.state === 'resolved' || value.state === 'confirmable' ? null
        : value.state === 'absent' ? 'The original request has no recorded outcome. You may explicitly retry that exact request.'
          : 'The preview outcome is unknown. Check the original request or explicitly retry its retained meaning.' });
    } catch {
      if (lifetime.active()) store.setState({ saved: null, outcome: null, message: 'This preview could not be verified. Recover its original status before continuing.' });
    } finally { if (lifetime.active()) store.setState({ busy: false }); }
  }
  function prepare(profile: string) {
    const state = store.getState();
    const inProgress = state.outcome && ['prepared', 'pending', 'running'].includes(state.outcome.receipt.state)
      || state.observations.some((item) => item.state === 'resolved' && item.intent.kind !== 'enrollment_command'
        && 'receipt' in item.result && ['prepared', 'pending', 'running'].includes(item.result.receipt.state));
    if (!enabled || state.busy || state.message || state.nextIntent || state.recoveryOverflow || state.recoveryConflict || inProgress
      || state.observations.some((item) => item.state !== 'resolved')) return Promise.resolve();
    const body = { schema_version: 'text-analysis-preview-prepare.v1' as const, external_key: crypto.randomUUID(), expected_profile_sha256: profile };
    return observe((lifetime) => client.prepare(body, lifetime));
  }
  function confirm() {
    const state = store.getState(), outcome = state.outcome;
    if (!outcome?.confirmation || outcome.receipt.state !== 'prepared' || state.message || state.nextIntent || state.recoveryOverflow || state.recoveryConflict
      || state.observations.some((item) => item.state !== 'resolved')) return Promise.resolve();
    return observe((lifetime) => client.confirm(outcome.request.external_key, outcome.confirmation!, lifetime));
  }
  function recover(locator: ClientIntentCursor) {
    return locator.kind !== 'enrollment_command' ? observe((lifetime) => client.read(locator, lifetime)) : Promise.resolve();
  }
  function retry(value: TextAnalysisObservation) {
    if (value.state === 'resolved' || !value.intent || value.intent.kind === 'enrollment_command') return Promise.resolve();
    const intent = value.intent;
    return observe((lifetime) => client.retry(intent, lifetime));
  }
  async function openSaved(id: string) {
    if (!enabled || store.getState().busy) return;
    const lifetime = begin(); store.setState({ busy: true, saved: null, outcome: null, source: null, sourceError: null, sourceLoading: false, message: null });
    try {
      const output = await client.api.saved(id, lifetime.signal), expected = await expectedAccountRef(account!);
      if (!lifetime.active()) return;
      if (!output.output.summaries.every((summary) => summary.evidence.every((entry) => entry.reference.account_ref === expected))) throw new Error('Account changed');
      store.setState({ saved: output });
    } catch { if (lifetime.active()) store.setState({ message: 'This saved preview is unavailable or its sources could not be verified.' }); }
    finally { if (lifetime.active()) store.setState({ busy: false }); }
  }
  async function openSource(reference: QuestionEvidence) {
    const output = store.getState().saved;
    if (!enabled || store.getState().busy || !output?.output.summaries.some((item) => item.evidence.some((entry) => evidenceKey(entry.reference) === evidenceKey(reference)))) return;
    const lifetime = begin(); store.setState({ source: null, sourceLoading: true, sourceError: null });
    try {
      const source = parseSourceEvidence(await client.api.evidence(reference, lifetime.signal), reference);
      if (lifetime.active()) store.setState({ source, sourceLoading: false });
    } catch { if (lifetime.active()) store.setState({ saved: null, source: null, sourceLoading: false, sourceError: 'This source is no longer available or could not be verified.' }); }
  }
  function closeSource() {
    const state = store.getState();
    if (state.source || state.sourceLoading || state.sourceError) invalidate();
    store.setState({ source: null, sourceLoading: false, sourceError: null });
  }
  function dispose() { invalidate(); key = ''; account = null; enabled = false; store.setState({ ...initial }); }
  return { ...store, actions: { setContext, refresh, prepare, confirm, recover, retry, openSaved, openSource, closeSource, dispose } };
}
export type TextAnalysisPreviewStore = ReturnType<typeof createTextAnalysisPreviewStore>;
