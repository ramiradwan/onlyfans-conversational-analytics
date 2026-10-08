import { createStore } from 'zustand';

import {
  evidenceKey, expectedAccountRef, QuestionContractError, snapshotKey,
  type QuestionCatalog, type QuestionEvidence, type QuestionPlan, type QuestionResult, type SourceEvidence,
} from '../analytics/questionContract';
import type { MessagePageResponse } from '../protocol';
import type { BridgeTransportState } from './transportStore';
import { createMessageApi, type MessageApi } from '../services/messageApi';
import { createQuestionApi, QuestionApiError, type QuestionApi } from '../services/questionApi';

type Context = Pick<BridgeTransportState, 'creatorAccountId' | 'session' | 'viewRevision' | 'projection' | 'connection' | 'readModelState'>;
export interface QuestionState {
  status: 'idle' | 'loading' | 'running' | 'ready' | 'building' | 'unavailable' | 'stale' | 'error';
  message: string | null;
  catalog: QuestionCatalog | null;
  result: QuestionResult | null;
  source: SourceEvidence | null;
  sourceLoading: boolean;
  thread: MessagePageResponse | null;
  threadLoading: boolean;
  sourceError: string | null;
  pageNumber: number;
  canRun: boolean;
}
const initial: QuestionState = { status: 'idle', message: null, catalog: null,
  result: null, source: null, sourceLoading: false, thread: null,
  threadLoading: false, sourceError: null, pageNumber: 1, canRun: false };

export function createQuestionStore(api: QuestionApi = createQuestionApi(), messages: MessageApi = createMessageApi()) {
  const store = createStore<QuestionState>(() => ({ ...initial }));
  let context: Context | null = null;
  let contextKey = '';
  let sequence = 0;
  let request: AbortController | null = null;
  let expiry: ReturnType<typeof setTimeout> | null = null;
  let requests: QuestionPlan[] = [];
  let selectedSnapshot: string | null = null;
  let expiresAt = 0;
  const abort = () => { sequence += 1; request?.abort(); request = null; };
  const clearTimer = () => { if (expiry !== null) clearTimeout(expiry); expiry = null; };
  function invalidate(message: string) {
    abort(); clearTimer(); requests = []; selectedSnapshot = null; expiresAt = 0;
    store.setState({ result: null, source: null, thread: null, sourceError: null,
      sourceLoading: false, threadLoading: false, status: 'stale', message, pageNumber: 1 });
  }
  const begin = () => { abort(); request = new AbortController(); return { id: sequence, signal: request.signal }; };
  const current = (id: number) => context !== null && sequence === id;
  function failed(error: unknown, id: number) {
    if (!current(id)) return;
    clearTimer();
    const known = error instanceof QuestionApiError;
    store.setState({ status: known ? error.state : 'error',
      message: known || error instanceof QuestionContractError ? error.message : 'The question could not be completed. Try again.',
      result: null, source: null, thread: null, sourceLoading: false, threadLoading: false });
  }
  async function catalog() {
    if (!context?.creatorAccountId) return;
    const run = begin();
    store.setState({ status: 'loading', catalog: null });
    try {
      const value = await api.catalog(run.signal);
      if (current(run.id)) store.setState({ catalog: value, status: 'idle', message: null });
    } catch (error) { failed(error, run.id); }
  }
  function setContext(next: Context) {
    const key = JSON.stringify([next.creatorAccountId, next.session?.connection_id,
      next.viewRevision, next.projection.canonical_revision, next.projection.projected_at,
      next.projection.status, next.connection, next.readModelState]);
    if (key === contextKey) return;
    const sameSession = context?.creatorAccountId === next.creatorAccountId
      && context?.session?.connection_id === next.session?.connection_id;
    const hadResult = store.getState().result !== null || store.getState().status === 'running';
    invalidate(hadResult ? 'Messages changed. Run the question again.' : '');
    context = next; contextKey = key;
    const canRun = Boolean(next.creatorAccountId && next.viewRevision !== null
      && next.connection === 'connected' && next.readModelState === 'realtime'
      && next.projection.status === 'current');
    store.setState({ canRun, catalog: sameSession ? store.getState().catalog : null,
      status: canRun ? (hadResult ? 'stale' : 'idle') : 'unavailable',
      message: canRun ? (hadResult ? 'Messages changed. Run the question again.' : null)
        : 'Questions will be available when your saved messages have finished loading.' });
    if (canRun && !store.getState().catalog) void catalog();
  }
  async function page(plan: QuestionPlan, number: number) {
    const account = context?.creatorAccountId;
    if (!account || !store.getState().canRun) return;
    if (!store.getState().catalog?.questions.some((q) => q.question === plan.question && q.enabled)) return;
    const run = begin(); clearTimer();
    store.setState({ status: 'running', message: null, result: null, source: null,
      thread: null, sourceError: null, sourceLoading: false, threadLoading: false });
    try {
      const expected = await expectedAccountRef(account);
      if (!current(run.id)) return;
      const result = await api.answer(plan, run.signal);
      if (!current(run.id)) return;
      if (result.question.account_ref !== expected || result.question.snapshot.source_revision !== context?.projection.canonical_revision
        || (selectedSnapshot !== null && snapshotKey(result) !== selectedSnapshot)) throw new QuestionContractError();
      const due = result.question.snapshot.retention_due_at;
      expiresAt = Math.min(expiresAt || Date.now() + 15 * 60_000, due ? Date.parse(due) : Infinity);
      const lifetime = expiresAt - Date.now();
      if (lifetime <= 0) throw new QuestionApiError('stale', 'These results expired. Run the question again.');
      selectedSnapshot = snapshotKey(result);
      requests[number - 1] = { ...plan, cutoff: result.question.cutoff };
      store.setState({ result, pageNumber: number, status: 'ready' });
      expiry = setTimeout(() => invalidate('These results expired. Run the question again.'), lifetime);
    } catch (error) { failed(error, run.id); }
  }
  async function run(plan: QuestionPlan) { requests = []; selectedSnapshot = null; expiresAt = 0; await page(plan, 1); }
  async function next() {
    const { result, pageNumber } = store.getState();
    if (result?.next_cursor && pageNumber < 50) await page({ ...requests[pageNumber - 1], cursor: result.next_cursor }, pageNumber + 1);
  }
  async function previous() { const n = store.getState().pageNumber; if (n > 1) await page(requests[n - 2], n - 1); }
  async function openSource(reference: QuestionEvidence) {
    const result = store.getState().result;
    if (!result?.page.rows.some((r) => r.evidence.some((e) => evidenceKey(e) === evidenceKey(reference)))) return;
    const run = begin();
    store.setState({ source: null, sourceLoading: true, thread: null, sourceError: null });
    try {
      const source = await api.evidence(reference, run.signal);
      if (current(run.id)) store.setState({ source, sourceLoading: false });
    } catch (error) { failed(error, run.id); }
  }
  async function openConversation(older = false) {
    const { source, result, thread } = store.getState();
    const account = context?.creatorAccountId;
    if (!source || !result || !account || (older && !thread?.older_cursor)) return;
    const run = begin();
    store.setState({ thread: null, threadLoading: true, sourceError: null });
    try {
      await api.evidence(source.reference, run.signal);
      if (!current(run.id)) return;
      const loaded = await messages.getPage({ conversationId: source.location.conversation_id,
        before: older ? thread?.older_cursor : null, limit: 50, signal: run.signal });
      if (!current(run.id)) return;
      if (loaded.creator_account_id !== account || loaded.conversation_id !== source.location.conversation_id
        || loaded.projection.canonical_revision !== result.question.snapshot.source_revision
        || loaded.projection.projected_revision !== result.question.snapshot.source_revision
        || loaded.projection.status !== 'current') throw new QuestionContractError();
      store.setState({ thread: loaded, threadLoading: false });
    } catch (error) { failed(error, run.id); }
  }
  function closeSource() {
    abort();
    store.setState({ source: null, sourceLoading: false, thread: null, threadLoading: false, sourceError: null });
  }
  function dispose() {
    abort(); clearTimer(); context = null; contextKey = ''; requests = []; selectedSnapshot = null; expiresAt = 0;
    store.setState({ ...initial });
  }
  return { ...store, actions: { setContext, catalog, run, next, previous, openSource, openConversation,
    closeSource, dispose, invalidate } };
}
export type QuestionStore = ReturnType<typeof createQuestionStore>;
