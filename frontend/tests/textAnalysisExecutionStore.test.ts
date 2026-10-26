import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import viewFixture from '../../shared/nlp/application/v1/fixtures/enrollment-view.json';
import retainedFixture from '../../shared/nlp/application/v1/fixtures/client-enrollment-record.json';
import resultFixture from '../../shared/nlp/application/v1/fixtures/enrollment-result.json';
import { parseEnrollmentView, textAnalysisDigest, type ClientIntentRecord } from '../src/analytics/textAnalysisContract';
import type { TextAnalysisApi } from '../src/services/textAnalysisApi';
import { createTextAnalysisClient } from '../src/services/textAnalysisClient';
import { createTextAnalysisExecutionStore, type TextAnalysisExecutionStore } from '../src/store/textAnalysisExecutionStore';
import { createBridgeTransportStore } from '../src/store/transportStore';

let store: TextAnalysisExecutionStore;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { store?.actions.dispose(); vi.unstubAllGlobals(); });
function context() {
  return { ...createBridgeTransportStore().getState(), creatorAccountId: 'example-account', connection: 'connected' as const,
    session: { creator_account_id: 'example-account', connection_id: 'connection-a', bridge_session_id: 'session-a', negotiated_protocol_version: '2' as const, server_version: '1' } };
}
function fixture() {
  let view = parseEnrollmentView(viewFixture);
  const records = new Map<string, ClientIntentRecord>();
  const api = {
    current: vi.fn<TextAnalysisApi['current']>(async () => view),
    command: vi.fn<TextAnalysisApi['command']>(async (body) => {
      view = { ...view, current: { ...view.current, mode_version: view.current.mode_version + 1, state: body.action === 'pause' ? 'paused' : 'enabled' } };
      return { current: view.current, receipt: { command_key: body.command_key, meaning_sha256: await textAnalysisDigest(body),
        enrollment_id: view.current.enrollment_id, state: view.current.state, mode_version: view.current.mode_version } };
    }),
    commandReceipt: vi.fn<TextAnalysisApi['commandReceipt']>().mockResolvedValue(null),
    prepare: vi.fn<TextAnalysisApi['prepare']>(), outcome: vi.fn<TextAnalysisApi['outcome']>(), confirm: vi.fn<TextAnalysisApi['confirm']>(),
    savedList: vi.fn<TextAnalysisApi['savedList']>(), saved: vi.fn<TextAnalysisApi['saved']>(), evidence: vi.fn<TextAnalysisApi['evidence']>(),
    retain: vi.fn<TextAnalysisApi['retain']>(async (intent) => {
      const record = { ...intent, schema_version: 'text-analysis-client-intent-record.v1' as const,
        body_sha256: await textAnalysisDigest(intent.body), retained_at: '2026-10-25T00:00:00Z' } as ClientIntentRecord;
      records.set(`${intent.kind}:${intent.key}`, record); return record;
    }),
    retained: vi.fn<TextAnalysisApi['retained']>(async (kind, key) => records.get(`${kind}:${key}`) ?? null),
    retainedList: vi.fn<TextAnalysisApi['retainedList']>().mockResolvedValue({ items: [], next_after: null }),
  } satisfies TextAnalysisApi;
  store = createTextAnalysisExecutionStore(createTextAnalysisClient(api));
  store.actions.setContext(context(), 'creator-ceo', 'example-account');
  return { api, setView: (next: typeof view) => { view = next; } };
}
const ready = () => vi.waitFor(() => expect(store.getState()).toMatchObject({ busy: false, status: 'ready' }));
describe('durable account processing state', () => {
  it('loads without mutations and submits the captured enrollment/version through the coordinator', async () => {
    const { api } = fixture(); await ready();
    expect(api.retain).not.toHaveBeenCalled(); expect(api.command).not.toHaveBeenCalled();
    const command = store.actions.buildCommand('pause')!;
    expect(command).toMatchObject({ expected_enrollment_id: viewFixture.current.enrollment_id, expected_mode_version: 1, action: 'pause' });
    await store.actions.submit(command);
    expect(api.command).toHaveBeenCalledWith(command, expect.any(AbortSignal));
    expect(store.getState().view?.current).toMatchObject({ state: 'paused', mode_version: 2 });
  });
  it('preserves a lost-retention original body across empty refresh and explicit same-key retry', async () => {
    const { api } = fixture(); await ready();
    api.retain.mockRejectedValueOnce(new Error('not received'));
    const command = store.actions.buildCommand('pause')!; await store.actions.submit(command);
    await store.actions.refresh();
    const observation = store.getState().observations[0];
    expect(observation).toMatchObject({ state: 'unknown', intent: { key: command.command_key, body: command } });
    expect(store.actions.buildCommand('pause')).toBeNull(); expect(api.command).not.toHaveBeenCalled();
    await store.actions.recover(observation.locator);
    const checked = store.getState().observations[0];
    expect(checked).toMatchObject({ state: 'unretained', intent: { key: command.command_key, body: command } });
    await store.actions.retry(checked);
    expect(api.retain.mock.calls.map(([value]) => value.key)).toEqual([command.command_key, command.command_key]);
    expect(api.command).toHaveBeenCalledTimes(1);
  });
  it('refuses a different recovered meaning without replacing the original intent', async () => {
    const { api } = fixture(); await ready(); api.retain.mockRejectedValueOnce(new Error('unknown'));
    const body = store.actions.buildCommand('pause')!; await store.actions.submit(body);
    const changed = { ...body, action: 'retire' as const };
    api.retained.mockResolvedValueOnce({ schema_version: 'text-analysis-client-intent-record.v1', kind: 'enrollment_command', key: body.command_key,
      body: changed, retained_at: '2026-10-25T00:00:00Z', body_sha256: await textAnalysisDigest(changed) });
    await store.actions.recover({ kind: 'enrollment_command', key: body.command_key });
    expect(store.getState().recoveryConflict).toBe(true);
    expect(store.getState().observations[0].intent?.body).toEqual(body);
    expect(store.actions.buildCommand('pause')).toBeNull(); expect(api.command).not.toHaveBeenCalled();
  });
  it('fails closed at the recovery bound without evicting an unresolved original', async () => {
    const { api } = fixture(); await ready();
    const body = store.actions.buildCommand('pause')!;
    const intent = { schema_version: 'text-analysis-client-intent.v1' as const, kind: 'enrollment_command' as const, key: body.command_key, body };
    store.setState({ observations: Array.from({ length: 4096 }, (_, index) => {
      const key = `${String(index).padStart(8, '0')}-0000-4000-8000-000000000000`;
      return { state: 'unretained' as const, locator: { kind: intent.kind, key }, intent: { ...intent, key, body: { ...body, command_key: key } } };
    }) });
    const original = store.getState().observations[0];
    api.retainedList.mockResolvedValueOnce({ items: [retainedFixture as ClientIntentRecord], next_after: null }); await store.actions.refresh();
    expect(store.getState().recoveryOverflow).toBe(true); expect(store.getState().observations[0]).toBe(original);
    expect(store.getState().observations).toHaveLength(4096); expect(store.actions.buildCommand('pause')).toBeNull();
  });
  it('preserves unresolved keys across keyset pages and blocks a new request before discovery completes', async () => {
    const { api } = fixture(); await ready();
    const first = structuredClone(retainedFixture) as ClientIntentRecord;
    api.retainedList.mockResolvedValueOnce({ items: [first], next_after: { kind: first.kind, key: first.key } });
    await store.actions.refresh();
    expect(store.getState().observations[0].state).toBe('absent'); expect(store.actions.buildCommand('pause')).toBeNull();
    api.retainedList.mockResolvedValueOnce({ items: [], next_after: null });
    await store.actions.refresh({ kind: first.kind, key: first.key });
    expect(store.getState().observations[0].locator.key).toBe(first.key); expect(store.actions.buildCommand('pause')).toBeNull();
    api.retainedList.mockRejectedValueOnce(new Error('unavailable')); await store.actions.refresh();
    expect(store.getState().observations[0].locator.key).toBe(first.key);
  });
  it.each(['account', 'role', 'session', 'disconnect'] as const)('fences a delayed pre-dispatch lookup after %s changes', async (change) => {
    const { api } = fixture(); await ready(); let release!: () => void;
    api.retained.mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve(null); }));
    const command = store.actions.buildCommand('pause')!, work = store.actions.submit(command);
    await vi.waitFor(() => expect(release).toBeTypeOf('function'));
    const next = context();
    if (change === 'account') next.creatorAccountId = 'another-account';
    if (change === 'session') next.session.bridge_session_id = 'session-b';
    if (change === 'disconnect') store.actions.setContext({ ...next, connection: 'disconnected', session: null }, 'creator-ceo', 'example-account');
    else store.actions.setContext(next, change === 'role' ? 'operator' : 'creator-ceo', 'example-account');
    release(); await work;
    expect(api.retain).not.toHaveBeenCalled(); expect(api.command).not.toHaveBeenCalled();
    expect(store.getState().observations).toEqual([]);
  });
  it('keeps current state separate from an old command receipt and refuses a captured stale version', async () => {
    const { api, setView } = fixture(); await ready(); const old = store.actions.buildCommand('pause')!;
    setView({ ...parseEnrollmentView(viewFixture), current: { ...parseEnrollmentView(viewFixture).current, mode_version: 9, state: 'paused' } });
    api.retainedList.mockResolvedValueOnce({ items: [retainedFixture as ClientIntentRecord], next_after: null });
    api.commandReceipt.mockResolvedValueOnce(resultFixture as Awaited<ReturnType<TextAnalysisApi['commandReceipt']>>);
    await store.actions.refresh(); expect(store.getState().view?.current.mode_version).toBe(9);
    await store.actions.submit(old); expect(api.command).not.toHaveBeenCalled();
    expect(store.getState().observations[0].state).toBe('resolved');
  });
});
