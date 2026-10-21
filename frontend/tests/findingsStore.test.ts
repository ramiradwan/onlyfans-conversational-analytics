import { webcrypto } from 'node:crypto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { FindingsClient } from '../src/analytics/findingsClient';
import { parseFindings, type FindingsResponse } from '../src/analytics/findingsContract';
import type { SourceEvidence } from '../src/analytics/questionContract';
import { createFindingsStore, type FindingsStore } from '../src/store/findingsStore';
import { findingsBaselineFixture as baseline, findingsResponse, findingsSource } from '../src/story-only/findingsFixtures';
import { context } from './questionFixture';

let store: FindingsStore;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { store?.actions.dispose(); vi.unstubAllGlobals(); });
function setup(overrides: Partial<FindingsClient> = {}) {
  const api: FindingsClient = { read: vi.fn(async (request) => parseFindings(findingsResponse(request), request)),
    evidence: vi.fn(async (reference) => findingsSource(reference)), ...overrides };
  store = createFindingsStore(api); store.actions.setContext(baseline, context(), 'all');
  return api;
}
const ready = async () => vi.waitFor(() => expect(store.getState().status).toBe('ready'));

describe('findings store', () => {
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
  it('binds conversation reads and aborts all work on disposal', async () => {
    const api = setup(); await ready();
    const ref = store.getState().result!.conversation_page.items[0].conversation_ref;
    store.actions.setContext(baseline, context(), 'all', ref); await ready();
    expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ conversation_ref: ref }), expect.any(AbortSignal));
    store.actions.dispose(); expect(store.getState().result).toBeNull(); expect(store.getState().source).toBeNull();
  });
});
