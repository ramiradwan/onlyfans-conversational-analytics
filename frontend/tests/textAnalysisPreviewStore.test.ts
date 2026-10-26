import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import outcomeFixture from '../../shared/nlp/application/v1/fixtures/preview-outcome.json';
import savedFixture from '../../shared/nlp/application/v1/fixtures/preview-saved.json';
import prepareRecord from '../../shared/nlp/application/v1/fixtures/client-prepare-intent.json';
import { parseSavedPreview, textAnalysisDigest, verifyPreviewOutcome, type ClientIntentRecord, type PreviewOutcome } from '../src/analytics/textAnalysisContract';
import type { TextAnalysisApi } from '../src/services/textAnalysisApi';
import { createTextAnalysisClient } from '../src/services/textAnalysisClient';
import { createTextAnalysisPreviewStore, type TextAnalysisPreviewStore } from '../src/store/textAnalysisPreviewStore';
import { createBridgeTransportStore } from '../src/store/transportStore';

let store: TextAnalysisPreviewStore;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { store?.actions.dispose(); vi.unstubAllGlobals(); });
function context() {
  return { ...createBridgeTransportStore().getState(), creatorAccountId: 'example-account', connection: 'connected' as const,
    session: { creator_account_id: 'example-account', connection_id: 'connection-a', bridge_session_id: 'session-a', negotiated_protocol_version: '2' as const, server_version: '1' } };
}
async function fixture() {
  let outcome: PreviewOutcome | null = null;
  const saved = await parseSavedPreview(savedFixture, savedFixture.output.contribution_id), records = new Map<string, ClientIntentRecord>();
  const api = {
    current: vi.fn<TextAnalysisApi['current']>(), command: vi.fn<TextAnalysisApi['command']>(), commandReceipt: vi.fn<TextAnalysisApi['commandReceipt']>(),
    prepare: vi.fn<TextAnalysisApi['prepare']>(async (body) => {
      outcome = await verifyPreviewOutcome({ ...outcomeFixture, request: body,
        confirmation: { ...outcomeFixture.confirmation, external_key: body.external_key } }, body.external_key); return outcome;
    }),
    outcome: vi.fn<TextAnalysisApi['outcome']>(async (key) => outcome?.request.external_key === key ? outcome : null),
    confirm: vi.fn<TextAnalysisApi['confirm']>(async () => { outcome = { ...outcome!, receipt: { ...outcome!.receipt, state: 'pending' } }; return outcome; }),
    savedList: vi.fn<TextAnalysisApi['savedList']>().mockResolvedValue({ items: [{ contribution_id: saved.output.contribution_id }], next_after: null }),
    saved: vi.fn<TextAnalysisApi['saved']>().mockResolvedValue(saved),
    evidence: vi.fn<TextAnalysisApi['evidence']>(async (_contributionId, reference) => ({ reference,
      location: { conversation_id: 'example-conversation', message_id: 'example-message' }, text: '😀 echo echo', direction: 'inbound',
      checked_at: '2026-10-25T00:00:00Z', browser_span: reference.span ? { start: reference.span.start + 1, end: reference.span.end + 1 } : null })),
    retain: vi.fn<TextAnalysisApi['retain']>(async (intent) => {
      const record = { ...intent, schema_version: 'text-analysis-client-intent-record.v1' as const,
        body_sha256: await textAnalysisDigest(intent.body), retained_at: '2026-10-25T00:00:00Z' } as ClientIntentRecord;
      records.set(`${intent.kind}:${intent.key}`, record); return record;
    }),
    retained: vi.fn<TextAnalysisApi['retained']>(async (kind, key) => records.get(`${kind}:${key}`) ?? null),
    retainedList: vi.fn<TextAnalysisApi['retainedList']>().mockResolvedValue({ items: [], next_after: null }),
  } satisfies TextAnalysisApi;
  store = createTextAnalysisPreviewStore(createTextAnalysisClient(api));
  store.actions.setContext(context(), 'creator-ceo', 'example-account');
  await vi.waitFor(() => expect(store.getState().busy).toBe(false));
  return { api, saved };
}
describe('preview recovery and saved output state', () => {
  it('discovers without mutation or projection readiness, then reads only the selected saved output', async () => {
    const { api, saved } = await fixture();
    expect(context().projection.status).not.toBe('current');
    expect(api.prepare).not.toHaveBeenCalled(); expect(api.retain).not.toHaveBeenCalled(); expect(api.current).not.toHaveBeenCalled();
    await store.actions.openSaved(saved.output.contribution_id);
    expect(store.getState().saved).toEqual(saved); expect(api.saved).toHaveBeenCalledTimes(1);
  });
  it('requires explicit confirmation of the original prepared scope and never confirms recovery reads', async () => {
    const { api } = await fixture();
    await store.actions.prepare(outcomeFixture.request.expected_profile_sha256);
    const original = store.getState().outcome!;
    expect(original.receipt.state).toBe('prepared'); expect(api.confirm).not.toHaveBeenCalled();
    await store.actions.recover({ kind: 'preview_prepare', key: original.request.external_key });
    expect(api.confirm).not.toHaveBeenCalled();
    await store.actions.confirm();
    expect(api.confirm).toHaveBeenCalledWith(original.request.external_key, original.confirmation, expect.any(AbortSignal));
    expect(store.getState().outcome?.receipt.state).toBe('pending');
  });
  it('preserves a lost-retain intent through empty refresh and retries only its original key', async () => {
    const { api } = await fixture(); api.retain.mockRejectedValueOnce(new Error('unknown retention'));
    await store.actions.prepare(outcomeFixture.request.expected_profile_sha256);
    const before = store.getState().observations[0]; expect(before.state).toBe('unknown');
    await store.actions.refresh();
    expect(store.getState().observations[0]).toEqual(before);
    await store.actions.prepare(outcomeFixture.request.expected_profile_sha256); expect(api.retain).toHaveBeenCalledTimes(1);
    await store.actions.recover(before.locator);
    const checked = store.getState().observations[0]; expect(checked).toMatchObject({ state: 'unretained', intent: before.intent });
    await store.actions.retry(checked);
    expect(api.retain.mock.calls[1][0].key).toBe(before.locator.key); expect(api.prepare).toHaveBeenCalledTimes(1);
  });
  it('blocks a new preview until keyset recovery completes and preserves unresolved previous pages', async () => {
    const { api } = await fixture();
    const record = { ...prepareRecord, schema_version: 'text-analysis-client-intent-record.v1',
      body_sha256: await textAnalysisDigest(prepareRecord.body), retained_at: '2026-10-25T00:00:00Z' } as ClientIntentRecord;
    api.retainedList.mockResolvedValueOnce({ items: [record], next_after: { kind: record.kind, key: record.key } });
    await store.actions.refresh();
    await store.actions.prepare(outcomeFixture.request.expected_profile_sha256); expect(api.prepare).not.toHaveBeenCalled();
    api.retainedList.mockResolvedValueOnce({ items: [], next_after: null });
    await store.actions.refresh({ kind: record.kind, key: record.key });
    expect(store.getState().observations[0].locator.key).toBe(record.key);
    expect(store.getState().observations[0].state).toBe('absent');
  });
  it('preserves a prepared original across empty later pages and failures until its own read proves terminal', async () => {
    const { api } = await fixture(); await store.actions.prepare(outcomeFixture.request.expected_profile_sha256);
    const original = store.getState().outcome!, key = original.request.external_key;
    const record = await api.retained('preview_prepare', key); expect(record).not.toBeNull();
    api.retainedList.mockResolvedValueOnce({ items: [record!], next_after: { kind: 'preview_prepare', key } });
    await store.actions.refresh();
    api.retainedList.mockResolvedValueOnce({ items: [], next_after: null });
    await store.actions.refresh({ kind: 'preview_prepare', key });
    expect(store.getState().outcome).toEqual(original);
    expect(store.getState().observations[0]).toMatchObject({ state: 'resolved', result: { receipt: { state: 'prepared' } } });
    api.retainedList.mockRejectedValueOnce(new Error('unavailable')); await store.actions.refresh();
    await store.actions.prepare(outcomeFixture.request.expected_profile_sha256);
    expect(api.prepare).toHaveBeenCalledTimes(1); expect(api.retain).toHaveBeenCalledTimes(1);
    api.outcome.mockResolvedValueOnce({ ...original, receipt: { ...original.receipt, state: 'failed', terminal_reason: 'empty' } });
    await store.actions.recover({ kind: 'preview_prepare', key });
    expect(store.getState().outcome?.receipt.terminal_reason).toBe('empty');
    await store.actions.prepare(outcomeFixture.request.expected_profile_sha256);
    expect(api.prepare).toHaveBeenCalledTimes(2);
    expect(api.retain.mock.calls[1][0].key).not.toBe(key);
  });
  it('keeps complete empty and incomplete terminal outcomes separate and never fetches nonexistent output', async () => {
    const { api } = await fixture();
    api.prepare.mockImplementationOnce(async (body) => ({ ...await verifyPreviewOutcome({ ...outcomeFixture, request: body,
      confirmation: { ...outcomeFixture.confirmation, external_key: body.external_key } }, body.external_key),
      receipt: { ...outcomeFixture.receipt, state: 'failed', terminal_reason: 'empty' } }));
    await store.actions.prepare(outcomeFixture.request.expected_profile_sha256);
    expect(store.getState().outcome?.receipt).toMatchObject({ state: 'failed', terminal_reason: 'empty' });
    expect(api.saved).not.toHaveBeenCalled(); expect(store.getState().saved).toBeNull();
  });
  it('opens only shown references and verifies the selected repeated Unicode occurrence', async () => {
    const { api, saved } = await fixture(); await store.actions.openSaved(saved.output.contribution_id);
    const mention = saved.output.summaries.find((item) => item.kind === 'mention_category')!.evidence[1].reference;
    await store.actions.openSource({ ...mention, message_ref: `m1:${'f'.repeat(64)}` }); expect(api.evidence).not.toHaveBeenCalled();
    await store.actions.openSource(mention);
    expect(api.evidence).toHaveBeenCalledWith(saved.output.contribution_id, mention, expect.any(AbortSignal));
    expect(store.getState().source?.browser_span).toEqual({ start: 8, end: 12 });
    expect(store.getState().source?.text.slice(8, 12)).toBe('echo');
    api.evidence.mockRejectedValueOnce(new Error('source unavailable')); await store.actions.openSource(mention);
    expect(store.getState()).toMatchObject({ saved: null, source: null, sourceLoading: false });
  });
  it.each(['session', 'disconnect', 'role'] as const)('clears saved/source state and ignores delayed source reads after %s changes', async (change) => {
    const { api, saved } = await fixture(); await store.actions.openSaved(saved.output.contribution_id);
    const reference = saved.output.summaries[1].evidence[0].reference;
    let release!: () => void;
    const source = await api.evidence(saved.output.contribution_id, reference);
    api.evidence.mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve(source); }));
    const work = store.actions.openSource(reference);
    await vi.waitFor(() => expect(release).toBeTypeOf('function'));
    const next = context(); next.session.bridge_session_id = 'session-b';
    store.actions.setContext(change === 'disconnect' ? { ...next, session: null, connection: 'disconnected' } : next,
      change === 'role' ? 'operator' : 'creator-ceo', 'example-account');
    release(); await work; expect(store.getState()).toMatchObject({ saved: null, source: null, sourceLoading: false });
  });
});
