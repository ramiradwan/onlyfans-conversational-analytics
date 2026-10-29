import { webcrypto } from 'node:crypto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { FindingsClient } from '../src/analytics/findingsClient';
import { parseFindings, type FindingsResponse } from '../src/analytics/findingsContract';
import type { SourceEvidence } from '../src/analytics/questionContract';
import { createFindingsStore, type FindingsStore } from '../src/store/findingsStore';
import { findingsBaselineFixture as baseline, findingsResponse, findingsFilteredResponse, findingsSource, findingsSubsetResponse } from '../src/story-only/findingsFixtures';
import { context } from './questionFixture';

let store: FindingsStore;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { store?.actions.dispose(); vi.unstubAllGlobals(); });
function setup(overrides: Partial<FindingsClient> = {}) {
  const api: FindingsClient = { context: vi.fn(async () => { throw new Error('Unavailable'); }), read: vi.fn(async (request) => parseFindings(request.category_filter ? findingsFilteredResponse(request) : findingsResponse(request), request)),
    evidence: vi.fn(async (reference) => findingsSource(reference)), ...overrides };
  store = createFindingsStore(api); store.actions.setContext(baseline, context(), 'all');
  return api;
}
const ready = async () => vi.waitFor(() => expect(store.getState().status).toBe('ready'));

describe('findings store', () => {
  it('keeps raw context separate and clears it when the active category changes', async () => {
    setup({ context: vi.fn(async (request, source) => ({ schema_version: 'analytics-evidence-context.v1',
      reference: request.reference, projection_generation: request.expected_generation, projection_digest: request.expected_projection_digest,
      checked_at: source.checked_at, anchor_index: 0,
      items: [{ reference: source.reference, text: source.text, direction: source.direction, browser_span: source.browser_span }],
      has_earlier_stored_items: false, has_later_stored_items: true })) }); await ready();
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
    let resolve!: (value: Awaited<ReturnType<FindingsClient['context']>>) => void;
    const api = setup({ context: vi.fn(() => new Promise((done) => { resolve = done; })) }); await ready();
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

  it('owns category identity and resets both pages across task/category changes', async () => {
    const api = setup(); await ready();
    await store.actions.read(1, 1);
    const category = { task: 'message_topics' as const, category_id: 'synthetic-topic' };
    store.actions.setContext(baseline, context(), 'all', null, category);
    category.category_id = 'mutated';
    await ready();
    expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ evidence_offset: 0, conversation_offset: 0,
      category_filter: { task: 'message_topics', category_id: 'synthetic-topic' } }), expect.any(AbortSignal));
    await store.actions.read();
    expect(store.getState().result?.schema_version).toBe('analytics-findings.v3');
    store.actions.setContext(baseline, context(), 'all', null, { task: 'concept_mentions', category_id: 'synthetic-mention' });
    expect(store.getState().result).toBeNull(); await ready();
    expect(store.getState().result?.evidence_page.total).toBe(2);
  });
  it('aborts stale category reads and source dialogs before accepting a new category', async () => {
    let resolveRead!: (value: FindingsResponse) => void;
    const read = vi.fn<FindingsClient['read']>().mockImplementationOnce(() => new Promise((done) => { resolveRead = done; }))
      .mockImplementation(async (request) => parseFindings(findingsFilteredResponse(request), request));
    let resolveSource!: (value: SourceEvidence) => void;
    const evidence = vi.fn<FindingsClient['evidence']>(() => new Promise((done) => { resolveSource = done; }));
    setup({ read, evidence }); await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    const firstSignal = read.mock.calls[0][1] as AbortSignal;
    store.actions.setContext(baseline, context(), 'all', null, { task: 'concept_mentions', category_id: 'synthetic-mention' });
    await ready(); resolveRead(findingsResponse()); await Promise.resolve();
    expect(firstSignal.aborted).toBe(true);
    expect(store.getState().result?.evidence_page.total).toBe(2);
    const reference = store.getState().result!.evidence_page.items[0].reference;
    const pending = store.actions.openSource(reference);
    store.actions.setContext(baseline, context(), 'all', null, { task: 'message_topics', category_id: 'synthetic-topic' });
    expect(store.getState().source).toBeNull();
    resolveSource(findingsSource(reference)); await pending; await ready();
    expect((evidence.mock.calls[0][1] as AbortSignal).aborted).toBe(true);
    expect(store.getState().source).toBeNull();
    expect(store.getState().result?.evidence_page.items[0].task).toBe('message_topics');
  });
  it('reads only the accepted baseline and resolves only evidence from its current page', async () => {
    const api = setup(); await ready();
    expect(api.read).toHaveBeenCalledWith(expect.objectContaining({ baseline, direction: 'all', conversation_ref: null }), expect.any(AbortSignal));
    const ref = store.getState().result!.evidence_page.items[1].reference;
    await store.actions.openSource({ ...ref, message_ref: `m1:${'9'.repeat(64)}` });
    expect(api.evidence).not.toHaveBeenCalled();
    await store.actions.openSource(ref);
    expect(store.getState().source?.reference.span).toEqual({ start: 14, end: 18 });
  });
  it('aborts and discards a late summary after an account switch', async () => {
    let resolve!: (value: FindingsResponse) => void;
    const read = vi.fn<FindingsClient['read']>(() => new Promise<FindingsResponse>((done) => { resolve = done; }));
    setup({ read }); await vi.waitFor(() => expect(read).toHaveBeenCalled());
    const signal = read.mock.calls[0][1] as AbortSignal;
    store.actions.setContext(null, { ...context(), creatorAccountId: 'another-synthetic-account' }, 'all');
    resolve(findingsResponse()); await Promise.resolve();
    expect(signal.aborted).toBe(true); expect(store.getState().result).toBeNull(); expect(store.getState().source).toBeNull();
  });
  it('rejects a late generation while retaining the new generation result', async () => {
    let resolve!: (value: FindingsResponse) => void;
    const read = vi.fn<FindingsClient['read']>().mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
      .mockImplementation(async (request) => parseFindings(findingsResponse(request), request));
    setup({ read }); await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    store.actions.setContext({ ...baseline, projection_generation: 3 }, context(), 'all'); await ready();
    resolve(findingsResponse()); await Promise.resolve();
    expect(store.getState().result?.projection_generation).toBe(3);
  });
  it('discards a late summary after the requested dates change', async () => {
    let resolve!: (value: FindingsResponse) => void;
    const read = vi.fn<FindingsClient['read']>().mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
      .mockImplementation(async (request) => parseFindings(findingsResponse(request), request));
    setup({ read }); await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    const next = { ...baseline, requested_window: { ...baseline.requested_window, start: '2026-09-19T00:00:00.000Z' } };
    store.actions.setContext(next, context(), 'all'); await ready();
    resolve(findingsResponse()); await Promise.resolve();
    expect(store.getState().result?.requested_window.start).toBe(next.requested_window.start);
  });
  it('discards a pending source when filters or source revision change', async () => {
    let resolve!: (value: SourceEvidence) => void;
    const evidence = vi.fn(() => new Promise<SourceEvidence>((done) => { resolve = done; }));
    setup({ evidence }); await ready();
    const reference = store.getState().result!.evidence_page.items[1].reference;
    const pending = store.actions.openSource(reference);
    store.actions.setContext(baseline, context(), 'outbound');
    resolve(findingsSource(reference)); await pending;
    expect(store.getState().source).toBeNull(); await ready();
    expect(store.getState().result?.direction).toBe('outbound');
    store.actions.setContext(baseline, { ...context(), projection: { ...context().projection, canonical_revision: 8 } }, 'outbound');
    expect(store.getState().result).toBeNull(); expect(store.getState().status).toBe('unavailable');
  });
  it('clears stale source text and refuses a changed source version', async () => {
    const api = setup(); await ready();
    const ref = store.getState().result!.evidence_page.items[1].reference;
    await store.actions.openSource(ref); expect(store.getState().source).not.toBeNull();
    vi.mocked(api.evidence).mockResolvedValueOnce(findingsSource({ ...ref, source_version_digest: `sha256:${'9'.repeat(64)}` }));
    await store.actions.openSource(ref);
    expect(store.getState().source).toBeNull(); expect(store.getState().sourceError).toMatch(/may have changed or been removed/);
  });
  it('refuses mixed immutable snapshots across evidence pages', async () => {
    const api = setup(); await ready();
    vi.mocked(api.read).mockImplementationOnce(async (request) => ({ ...findingsResponse(request), snapshot_sha256: `sha256:${'9'.repeat(64)}` }));
    await store.actions.read(1);
    expect(store.getState().result).toBeNull(); expect(store.getState().status).toBe('error');
  });
  it('retains a subset snapshot across pages and rejects a changed task declaration', async () => {
    const api = setup({ read: vi.fn(async (request) => parseFindings(findingsSubsetResponse(request), request)) }); await ready();
    expect(store.getState().result?.tasks.map((task) => task.task)).toEqual(['concept_mentions']);
    await store.actions.read(1); await ready();
    expect(store.getState().result?.evidence_page.items[0].reference.span).toEqual({ start: 14, end: 18 });
    vi.mocked(api.read).mockImplementationOnce(async (request) => parseFindings(findingsSubsetResponse(request, ['message_topics', 'concept_mentions']), request));
    await store.actions.read(1);
    expect(store.getState().result).toBeNull(); expect(store.getState().status).toBe('error');
  });
  it('binds conversation reads and aborts all work on disposal', async () => {
    const api = setup(); await ready();
    const ref = store.getState().result!.conversation_page.items[0].conversation_ref;
    store.actions.setContext(baseline, context(), 'all', ref); await ready();
    expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ conversation_ref: ref }), expect.any(AbortSignal));
    store.actions.dispose(); expect(store.getState().result).toBeNull(); expect(store.getState().source).toBeNull();
  });
});
