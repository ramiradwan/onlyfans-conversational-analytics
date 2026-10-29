import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { LiteralsClient } from '../src/analytics/literalsClient';
import { parseLiterals, type LiteralFindings } from '../src/analytics/literalsContract';
import type { SourceEvidence } from '../src/analytics/questionContract';
import { createLiteralsStore, type LiteralsStore } from '../src/store/literalsStore';
import { findingsBaselineFixture as baseline, findingsSource } from '../src/story-only/findingsFixtures';
import { literalsResponse } from '../src/story-only/literalsFixtures';
import { context } from './questionFixture';

let store: LiteralsStore;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { store?.actions.dispose(); vi.unstubAllGlobals(); });
function setup(overrides: Partial<LiteralsClient> = {}) {
  const api: LiteralsClient = { context: vi.fn(async () => { throw new Error('Unavailable'); }), read: vi.fn(async (r) => parseLiterals(literalsResponse(r), r)), evidence: vi.fn(async (r) => findingsSource(r)), ...overrides };
  store = createLiteralsStore(api); store.actions.setContext(baseline, context(), 'all'); return api;
}
const ready = () => vi.waitFor(() => expect(store.getState().status).toBe('ready'));
describe('literal findings store', () => {
  it('keeps raw context separate and clears it when the active category changes', async () => {
    setup({ context: vi.fn(async (request, source) => ({ schema_version: 'analytics-evidence-context.v1',
      reference: request.reference, projection_generation: request.expected_generation, projection_digest: request.expected_projection_digest,
      checked_at: source.checked_at, anchor_index: 0,
      items: [{ reference: source.reference, text: source.text, direction: source.direction, browser_span: source.browser_span }],
      has_earlier_stored_items: false, has_later_stored_items: true })) }); await store.actions.load();
    const result = store.getState().result!;
    await store.actions.openSource(result.evidence_page.items[0].reference);
    await store.actions.showContext();
    expect(store.getState().sourceContext?.items).toHaveLength(1);
    expect(store.getState().result).toBe(result);
    store.actions.setContext(baseline, context(), 'all', null, { task: 'message_topics', category_id: 'synthetic-topic' });
    expect(store.getState().sourceContext).toBeNull();
    expect(store.getState().source).toBeNull();
  });

  it('binds surrounding messages to the verified source and rejects late context after closing', async () => {
    let resolve!: (value: Awaited<ReturnType<LiteralsClient['context']>>) => void;
    const api = setup({ context: vi.fn(() => new Promise((done) => { resolve = done; })) }); await store.actions.load();
    const reference = store.getState().result!.evidence_page.items[0].reference;
    await store.actions.openSource(reference);
    const source = store.getState().source!;
    expect(source).not.toBeNull();
    const pending = store.actions.showContext();
    expect(api.context).toHaveBeenCalledWith(expect.objectContaining({ reference, expected_generation: baseline.projection_generation }), source, expect.any(AbortSignal));
    store.actions.closeSource();
    resolve({ schema_version: 'analytics-evidence-context.v1', reference, projection_generation: baseline.projection_generation,
      projection_digest: baseline.projection_digest, checked_at: source.checked_at, anchor_index: 0,
      items: [{ reference, text: source.text, direction: source.direction, browser_span: source.browser_span }],
      has_earlier_stored_items: false, has_later_stored_items: false });
    await pending;
    expect(store.getState().sourceContext).toBeNull();
    expect(store.getState().contextLoading).toBe(false);
  });

  it('is lazy, pins pagination/selection and retains ranked rows when selecting sources', async () => {
    const api = setup(); expect(api.read).not.toHaveBeenCalled(); await store.actions.load();
    const initial = store.getState().result!; await store.actions.selectValue(initial.row_page.items[0].node_id);
    expect(store.getState().result?.row_page).toEqual(initial.row_page);
    expect(store.getState().result?.evidence_page.total).toBe(2);
    expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ node_id: initial.row_page.items[0].node_id, expected_normalization_sha256: initial.normalization_sha256, evidence_offset: 0 }), expect.any(AbortSignal));
    await store.actions.pageRows(1);
    expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ row_offset: 1, expected_normalization_sha256: initial.normalization_sha256 }), expect.any(AbortSignal));
    await store.actions.selectValue(null); expect(store.getState().result?.evidence_page.total).toBe(3);
  });
  it('owns filter copies and aborts stale read and source responses on context changes', async () => {
    let resolve!: (v: LiteralFindings) => void;
    const read = vi.fn<LiteralsClient['read']>().mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
      .mockImplementation(async (r) => parseLiterals(literalsResponse(r), r));
    let resolveSource!: (v: SourceEvidence) => void;
    const evidence = vi.fn<LiteralsClient['evidence']>(() => new Promise((done) => { resolveSource = done; }));
    setup({ read, evidence }); const first = store.actions.load(); await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    const signal = read.mock.calls[0][1]!;
    const category = { task: 'concept_mentions' as const, category_id: 'synthetic-mention' };
    store.actions.setContext(baseline, context(), 'inbound', null, category); category.category_id = 'mutated'; await ready();
    resolve(literalsResponse()); await first; expect(signal.aborted).toBe(true);
    expect(store.getState().result?.direction).toBe('inbound');
    expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ category_filter: { task: 'concept_mentions', category_id: 'synthetic-mention' }, row_offset: 0, evidence_offset: 0 }), expect.any(AbortSignal));
    const reference = store.getState().result!.evidence_page.items[0].reference;
    const pending = store.actions.openSource(reference);
    store.actions.setContext(baseline, { ...context(), connection: 'disconnected' }, 'all');
    expect(store.getState().result).toBeNull(); expect(store.getState().source).toBeNull();
    resolveSource(findingsSource(reference)); await pending;
    expect(evidence.mock.calls[0][1]!.aborted).toBe(true); expect(store.getState().source).toBeNull();
  });
  it('rejects changed publication and refuses evidence outside the current page', async () => {
    const api = setup(); await store.actions.load(); const first = store.getState().result!;
    const other = structuredClone(first.evidence_page.items[0].reference); other.message_ref = `m1:${'f'.repeat(64)}`;
    await store.actions.openSource(other); expect(api.evidence).not.toHaveBeenCalled();
    vi.mocked(api.read).mockImplementationOnce(async (r) => { const value = literalsResponse(r); value.normalization_sha256 = `sha256:${'a'.repeat(64)}`; return value; });
    await store.actions.pageEvidence(1); expect(store.getState().status).toBe('error'); expect(store.getState().result).toBeNull();
  });
  it('invalidates a selected source immediately when the session changes', async () => {
    setup(); await store.actions.load(); await store.actions.openSource(store.getState().result!.evidence_page.items[0].reference);
    expect(store.getState().source).not.toBeNull();
    store.actions.setContext(baseline, { ...context(), creatorAccountId: 'other-account' }, 'all');
    expect(store.getState().source).toBeNull(); await vi.waitFor(() => expect(store.getState().status).toBe('error'));
    expect(store.getState().result).toBeNull();
  });
  it('rejects a highlighted surface that differs from a known literal value', async () => {
    setup({ evidence: async (r) => ({ ...findingsSource(r), text: findingsSource(r).text.replace('item', 'xxxx') }) });
    await store.actions.load(); await store.actions.openSource(store.getState().result!.evidence_page.items[0].reference);
    expect(store.getState().source).toBeNull(); expect(store.getState().sourceError).not.toBeNull();
  });
  it('rejects evidence total drift when returning to the first page', async () => {
    const api = setup(); await store.actions.load(); await store.actions.pageEvidence(1);
    vi.mocked(api.read).mockImplementationOnce(async (r) => {
      const value = literalsResponse(r); value.evidence_page.items.pop(); value.evidence_page.total -= 1;
      return parseLiterals(value, r);
    });
    await store.actions.pageEvidence(0);
    expect(store.getState().status).toBe('error'); expect(store.getState().result).toBeNull();
  });
});
