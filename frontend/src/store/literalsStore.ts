import { createStore } from 'zustand';

import type { BridgeTransportState } from './transportStore';
import type { FindingsBaseline } from '../analytics/analyticsReadModel';
import { FindingsClientError } from '../analytics/findingsClient';
import type { FindingsCategoryFilter, FindingsDirection } from '../analytics/findingsContract';
import { createLiteralsClient, type LiteralsClient } from '../analytics/literalsClient';
import { literalKey, literalsSnapshot, type LiteralFindings, type LiteralsRequest } from '../analytics/literalsContract';
import { evidenceKey, expectedAccountRef, parseSourceEvidence, type QuestionEvidence, type SourceEvidence } from '../analytics/questionContract';

type Context = Pick<BridgeTransportState, 'creatorAccountId' | 'session' | 'connection' | 'projection' | 'readModelState'>;
export interface LiteralsState {
  status: 'idle' | 'loading' | 'ready' | 'building' | 'unavailable' | 'error'; binding: string; result: LiteralFindings | null; message: string | null;
  source: SourceEvidence | null; sourceLoading: boolean; sourceError: string | null;
}
const initial: LiteralsState = { status: 'idle', binding: '', result: null, message: null, source: null, sourceLoading: false, sourceError: null };
export const literalsBinding = (baseline: FindingsBaseline | null, direction: FindingsDirection, conversation: string | null, category?: FindingsCategoryFilter | null) =>
  JSON.stringify([baseline, direction, conversation, category?.task ?? null, category?.category_id ?? null]);
export function createLiteralsStore(api: LiteralsClient = createLiteralsClient()) {
  const store = createStore<LiteralsState>(() => ({ ...initial }));
  let selected: LiteralsRequest | null = null; let account: string | null = null; let contextKey = ''; let sequence = 0;
  let controller: AbortController | null = null; let snapshot: string | null = null; let activated = false;
  const abort = () => { sequence += 1; controller?.abort(); controller = null; };
  const current = (id: number) => Boolean(selected && id === sequence);
  function invalidate() { abort(); store.setState({ result: null, source: null, sourceLoading: false, sourceError: null }); }
  async function read(rowOffset = 0, evidenceOffset = 0, node: string | null = null) {
    if (!selected || !account) return;
    const prior = store.getState().result;
    const request: LiteralsRequest = { ...selected, row_offset: rowOffset, evidence_offset: evidenceOffset, node_id: node };
    const accountId = account; abort(); controller = new AbortController(); const signal = controller.signal; const id = sequence;
    store.setState({ status: 'loading', result: null, source: null, sourceLoading: false, sourceError: null, message: null });
    try {
      const expected = await expectedAccountRef(accountId); if (!current(id)) return;
      if (expected !== request.baseline.account_ref) throw new Error('Account changed');
      const result = await api.read(request, signal); if (!current(id)) return;
      const identity = literalsSnapshot(result);
      if (snapshot !== null && snapshot !== identity) throw new Error('Literal publication changed');
      if (request.expected_normalization_sha256 !== null && prior && prior.node_id === node
        && prior.evidence_page.total !== result.evidence_page.total) throw new Error('Evidence changed');
      snapshot = identity;
      selected = { ...request, expected_normalization_sha256: result.normalization_sha256 };
      store.setState({ status: 'ready', result, message: null });
    } catch (error) {
      if (current(id)) store.setState({ result: null, status: error instanceof FindingsClientError ? error.state : 'error',
        message: error instanceof FindingsClientError ? error.message : 'These literal values could not be verified. Refresh the saved analysis.' });
    }
  }
  function setContext(baseline: FindingsBaseline | null, next: Context, direction: FindingsDirection, conversation: string | null = null, category: FindingsCategoryFilter | null = null) {
    const binding = literalsBinding(baseline, direction, conversation, category);
    const key = JSON.stringify([binding, next.creatorAccountId, next.session?.connection_id, next.connection, next.readModelState, next.projection]);
    if (key === contextKey) return;
    invalidate(); contextKey = key; snapshot = null; account = next.creatorAccountId;
    selected = baseline && account && next.connection === 'connected' && next.readModelState === 'realtime' && next.projection.status === 'current'
      && next.projection.canonical_revision === baseline.source_revision && next.projection.projected_revision === baseline.source_revision
      ? { baseline: structuredClone(baseline), direction, conversation_ref: conversation, category_filter: category ? { ...category } : null,
        row_offset: 0, row_limit: 50, evidence_offset: 0, evidence_limit: 50, node_id: null, expected_normalization_sha256: null } : null;
    store.setState({ ...initial, binding, status: selected ? 'idle' : 'unavailable', message: selected ? null : 'Literal values are unavailable for the current analytics view.' });
    if (activated && selected) void read();
  }
  function load() { activated = true; snapshot = null; if (selected) selected = { ...selected, expected_normalization_sha256: null }; return read(); }
  function pageRows(offset: number) { const r = store.getState().result; if (r) return read(offset, 0, r.node_id); }
  function pageEvidence(offset: number) { const r = store.getState().result; if (r) return read(r.row_page.offset, offset, r.node_id); }
  function selectValue(node: string | null) { const r = store.getState().result; if (r?.availability === 'available' && (node === null || r.row_page.items.some((v) => v.node_id === node))) return read(r.row_page.offset, 0, node); }
  async function openSource(reference: QuestionEvidence) {
    const result = store.getState().result;
    const finding = result?.evidence_page.items.find((v) => evidenceKey(v.reference) === evidenceKey(reference));
    if (!result || !finding) return;
    const row = result.row_page.items.find((v) => v.node_id === finding.node_id);
    abort(); controller = new AbortController(); const signal = controller.signal; const id = sequence;
    store.setState({ source: null, sourceLoading: true, sourceError: null });
    try {
      const source = parseSourceEvidence(await api.evidence(reference, signal), reference);
      if (!current(id)) return;
      if (selected?.direction !== 'all' && selected?.direction !== source.direction) throw new Error('Direction changed');
      if (row && literalKey(Array.from(source.text).slice(reference.span!.start, reference.span!.end).join('')) !== row.literal_key) throw new Error('Literal changed');
      store.setState({ source, sourceLoading: false });
    } catch { if (current(id)) store.setState({ source: null, sourceLoading: false, sourceError: 'This source message could not be verified. Refresh the saved values before trying again.' }); }
  }
  function closeSource() { abort(); store.setState({ source: null, sourceLoading: false, sourceError: null }); }
  function dispose() { invalidate(); selected = null; account = null; contextKey = ''; snapshot = null; activated = false; store.setState({ ...initial }); }
  return { ...store, actions: { setContext, load, pageRows, pageEvidence, selectValue, openSource, closeSource, invalidate, dispose } };
}
export type LiteralsStore = ReturnType<typeof createLiteralsStore>;
