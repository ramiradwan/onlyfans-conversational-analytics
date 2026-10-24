import { createStore } from 'zustand';

import type { BridgeTransportState } from './transportStore';
import type { FindingsBaseline } from '../analytics/analyticsReadModel';
import { createFindingsClient, FindingsClientError, type FindingsClient } from '../analytics/findingsClient';
import { findingsSnapshot, type FindingsCategoryFilter, type FindingsDirection, type FindingsRequest, type FindingsResponse } from '../analytics/findingsContract';
import { evidenceKey, expectedAccountRef, parseSourceEvidence, type QuestionEvidence, type SourceEvidence } from '../analytics/questionContract';

type Context = Pick<BridgeTransportState, 'creatorAccountId' | 'session' | 'connection' | 'projection' | 'readModelState'>;
export interface FindingsState {
  status: 'unavailable' | 'loading' | 'ready' | 'building' | 'stale' | 'error';
  binding: string;
  result: FindingsResponse | null;
  message: string | null;
  source: SourceEvidence | null;
  sourceLoading: boolean;
  sourceError: string | null;
}
const initial: FindingsState = { status: 'unavailable', binding: '', result: null,
  message: 'Text analysis is unavailable until a matching saved analysis is ready.',
  source: null, sourceLoading: false, sourceError: null };
export const findingsBinding = (baseline: FindingsBaseline | null, direction: FindingsDirection, conversation: string | null = null,
  category: FindingsCategoryFilter | null = null) => JSON.stringify([baseline, direction, conversation, category?.task ?? null, category?.category_id ?? null]);

export function createFindingsStore(api: FindingsClient = createFindingsClient()) {
  const store = createStore<FindingsState>(() => ({ ...initial }));
  let contextKey = '';
  let account: string | null = null;
  let selected: FindingsRequest | null = null;
  let sequence = 0;
  let controller: AbortController | null = null;
  let snapshot: string | null = null;
  const abort = () => { sequence += 1; controller?.abort(); controller = null; };
  const begin = () => { abort(); controller = new AbortController(); return { id: sequence, signal: controller.signal }; };
  const current = (id: number) => Boolean(selected && id === sequence);
  function invalidate() { abort(); store.setState({ result: null, source: null, sourceLoading: false, sourceError: null }); }
  async function read(offset = 0, conversationOffset = 0) {
    if (!selected || !account) return;
    const request = { ...selected, evidence_offset: offset, conversation_offset: conversationOffset };
    const accountId = account;
    const run = begin();
    store.setState({ status: 'loading', result: null, source: null, sourceLoading: false, sourceError: null, message: null });
    try {
      const expected = await expectedAccountRef(accountId);
      if (!current(run.id)) return;
      if (expected !== request.baseline.account_ref) throw new Error('Account changed');
      const result = await api.read(request, run.signal);
      if (!current(run.id)) return;
      const identity = findingsSnapshot(result);
      if ((offset > 0 || conversationOffset > 0) && snapshot !== identity) throw new Error('Snapshot changed');
      snapshot = identity;
      store.setState({ result, status: 'ready', message: null });
    } catch (error) {
      if (current(run.id)) store.setState({ result: null, status: error instanceof FindingsClientError ? error.state : 'error',
        message: error instanceof FindingsClientError ? error.message : 'These text analysis results could not be verified. Refresh the analytics view.' });
    }
  }
  function setContext(baseline: FindingsBaseline | null, next: Context, direction: FindingsDirection, conversation: string | null = null,
    category: FindingsCategoryFilter | null = null) {
    const binding = findingsBinding(baseline, direction, conversation, category);
    const key = JSON.stringify([binding, next.creatorAccountId, next.session?.connection_id,
      next.connection, next.readModelState, next.projection]);
    if (key === contextKey) return;
    invalidate(); snapshot = null; contextKey = key; account = next.creatorAccountId;
    selected = baseline && account && next.connection === 'connected' && next.readModelState === 'realtime'
      && next.projection.status === 'current' && next.projection.canonical_revision === baseline.source_revision
      && next.projection.projected_revision === baseline.source_revision
      ? { baseline, direction, evidence_offset: 0, evidence_limit: 50, conversation_ref: conversation, conversation_offset: 0, conversation_limit: 50,
        category_filter: category ? { task: category.task, category_id: category.category_id } : null } : null;
    store.setState({ ...initial, binding });
    if (selected) void read();
  }
  async function openSource(reference: QuestionEvidence) {
    const result = store.getState().result;
    if (!result?.evidence_page.items.some((item) => evidenceKey(item.reference) === evidenceKey(reference))) return;
    const run = begin();
    store.setState({ source: null, sourceLoading: true, sourceError: null });
    try {
      const source = parseSourceEvidence(await api.evidence(reference, run.signal), reference);
      if (selected?.direction !== 'all' && selected?.direction !== source.direction) throw new Error('Direction changed');
      if (current(run.id)) store.setState({ source, sourceLoading: false });
    } catch {
      if (current(run.id)) store.setState({ source: null, sourceLoading: false,
        sourceError: 'This source message could not be verified. It may have changed or been removed. Refresh the results before trying again.' });
    }
  }
  function closeSource() { abort(); store.setState({ source: null, sourceLoading: false, sourceError: null }); }
  function dispose() { invalidate(); contextKey = ''; selected = null; account = null; store.setState({ ...initial }); }
  return { ...store, actions: { setContext, read, invalidate, openSource, closeSource, dispose } };
}
export type FindingsStore = ReturnType<typeof createFindingsStore>;
