import { webcrypto } from 'node:crypto';

import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import confirmIntentExample from '../../shared/nlp/application/v1/fixtures/client-confirm-intent.json';
import outcomeExample from '../../shared/nlp/application/v1/fixtures/preview-outcome.json';
import prepareIntentExample from '../../shared/nlp/application/v1/fixtures/client-prepare-intent.json';
import savedExample from '../../shared/nlp/application/v1/fixtures/preview-saved.json';
import partialExample from '../../shared/nlp/application/v1/fixtures/preview-saved-partial.json';
import {
  clientIntent, parseSavedPreview, textAnalysisDigest, verifyPreviewOutcome,
  type ClientIntent, type ClientIntentRecord, type PreviewOutcome,
} from '../src/analytics/textAnalysisContract';
import type { EngineAcquisitionApi, EngineStatus } from '../src/api/engineAcquisition';
import { TextAnalysisPreviews } from '../src/components/analytics/TextAnalysisPreviews';
import type { NlpDependenciesApi, NlpDependencyStatus } from '../src/services/nlpDependenciesApi';
import { TextAnalysisApiError, type TextAnalysisApi } from '../src/services/textAnalysisApi';
import { createTextAnalysisClient } from '../src/services/textAnalysisClient';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const ACCOUNT = 'example-account', KEY = outcomeExample.request.external_key;
const clone = <T,>(value: T): T => structuredClone(value);
function engineFixture() {
  const idle: EngineStatus = { operation_id: null, state: 'not_requested', installed: false, in_flight: false,
    cancel_requested: false, deadline_reached: false, progress_percent: null, reason: null, hresult: null };
  const quote = { availability: 'available' as const, explicit_selection: 'preview' as const, review_digest: 'a'.repeat(64),
    engine_package_version: '1.0.42.0', expected_complete_package_bytes: 12345,
    declared_disk: { installed_space_budget_bytes: 4096, temporary_space_budget_bytes: 2048,
      rollback_space_budget_bytes: 0, required_free_space_bytes: 6144 } };
  return { selection: 'preview' as const, supported: vi.fn(() => true),
    status: vi.fn<EngineAcquisitionApi['status']>().mockResolvedValue(idle),
    review: vi.fn<EngineAcquisitionApi['review']>().mockResolvedValue(quote),
    start: vi.fn<EngineAcquisitionApi['start']>().mockResolvedValue({ ...idle, operation_id: 'b'.repeat(32), state: 'acquiring', in_flight: true }),
    cancel: vi.fn<EngineAcquisitionApi['cancel']>(), quote };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function connect(transport: ReturnType<typeof createBridgeTransportStore>, account = ACCOUNT, suffix = '1') {
  transport.bindAccount(account);
  transport.acceptSession({ connection_id: `10000000-0000-4000-8000-00000000000${suffix}`,
    bridge_session_id: `20000000-0000-4000-8000-00000000000${suffix}`, creator_account_id: account,
    negotiated_protocol_version: '2', server_version: 'example' });
}
async function retained(intent: ClientIntent): Promise<ClientIntentRecord> {
  return { ...intent, schema_version: 'text-analysis-client-intent-record.v1',
    body_sha256: await textAnalysisDigest(intent.body), retained_at: '2026-10-25T00:00:00Z' };
}
function fixture(partial = false, engineApi?: EngineAcquisitionApi) {
  let original: PreviewOutcome | null = null;
  const journal = new Map<string, ClientIntentRecord>();
  const api = {
    current: vi.fn<TextAnalysisApi['current']>(), command: vi.fn<TextAnalysisApi['command']>(),
    commandReceipt: vi.fn<TextAnalysisApi['commandReceipt']>().mockResolvedValue(null),
    prepare: vi.fn<TextAnalysisApi['prepare']>(async (request) => {
      original = await verifyPreviewOutcome(clone(outcomeExample), request.external_key, request); return clone(original);
    }),
    outcome: vi.fn<TextAnalysisApi['outcome']>(async () => original === null ? null : clone(original)),
    confirm: vi.fn<TextAnalysisApi['confirm']>(async () => {
      original = await verifyPreviewOutcome({ ...clone(outcomeExample), receipt: { ...outcomeExample.receipt, state: 'pending' } }, KEY);
      return clone(original);
    }),
    savedList: vi.fn<TextAnalysisApi['savedList']>().mockResolvedValue({ items: [{ contribution_id: savedExample.output.contribution_id }], next_after: null }),
    saved: vi.fn<TextAnalysisApi['saved']>(async (id) => parseSavedPreview(clone(partial ? partialExample : savedExample), id)),
    evidence: vi.fn<TextAnalysisApi['evidence']>(async (reference) => {
      const text = '😀 echo echo', points = Array.from(text), span = reference.span;
      return { reference, location: { conversation_id: 'example-chat', message_id: 'example-message' }, text, direction: 'inbound',
        checked_at: '2026-10-26T00:00:00Z', browser_span: span ? { start: points.slice(0, span.start).join('').length,
          end: points.slice(0, span.end).join('').length } : null };
    }),
    retain: vi.fn<TextAnalysisApi['retain']>(async (intent) => {
      const record = await retained(intent); journal.set(`${intent.kind}:${intent.key}`, record); return record;
    }),
    retained: vi.fn<TextAnalysisApi['retained']>(async (kind, key) => journal.get(`${kind}:${key}`) ?? null),
    retainedList: vi.fn<TextAnalysisApi['retainedList']>(async () => ({ items: [...journal.values()].sort((a, b) =>
      a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : a.key.localeCompare(b.key)), next_after: null })),
  } satisfies TextAnalysisApi;
  const status: NlpDependencyStatus = { configured: true, profiles: [{ profile_id: 'profile.synthetic', revision: '1',
    profile_sha256: outcomeExample.request.expected_profile_sha256, catalogued: true, installed: true, prepared: true,
    withdrawn: false, storage_state: 'installed', languages: ['en'], tasks: ['message_topics', 'concept_mentions'], resources: null }],
  available_profiles: [outcomeExample.request.expected_profile_sha256], failure: null,
  resume_granularity: 'verified_files_current_file_restarts', acquisitions: [], selected: null };
  const dependencies = { get: vi.fn<NlpDependenciesApi['get']>().mockResolvedValue(status),
    command: vi.fn<NlpDependenciesApi['command']>(), observe: vi.fn<NlpDependenciesApi['observe']>() } satisfies NlpDependenciesApi;
  const transport = createBridgeTransportStore(); connect(transport);
  const client = createTextAnalysisClient(api);
  const view = (account = ACCOUNT) => <ThemeProvider theme={theme}><TextAnalysisPreviews creatorAccountId={account}
    transport={transport} client={client} dependenciesApi={dependencies} engineApi={engineApi} /></ThemeProvider>;
  return { api, dependencies, transport, view, journal, setOutcome: (value: PreviewOutcome | null) => { original = value; } };
}
async function selectProfile() {
  await waitFor(() => expect((screen.getByRole('button', { name: 'Check retained previews' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Approved preview profile' }));
  fireEvent.click(await screen.findByRole('option', { name: 'profile.synthetic · 1' }));
}
async function openSaved() {
  const button = await screen.findByRole('button', { name: 'Open saved preview 1' });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(button);
  return screen.findByLabelText('Verified shown preview');
}
beforeEach(() => {
  vi.stubGlobal('crypto', { subtle: webcrypto.subtle, randomUUID: () => KEY });
  useUserStore.getState().actions.setUserRole('creator-ceo');
});
afterEach(() => { cleanup(); useUserStore.setState({ role: null, roleResolved: false }); vi.unstubAllGlobals(); });

describe('Text analysis preview controls', () => {
  it('reviews explicit preview Engine setup independently of preview execution and model acquisition', async () => {
    const engine = engineFixture(), x = fixture(false, engine); render(x.view());
    await screen.findByText('Engine installation has not been requested.');
    expect(document.querySelector('[data-journey-state="textanalysis.engine_controls"]')).toBeTruthy();
    expect(engine.status).toHaveBeenCalledOnce(); expect(engine.review).not.toHaveBeenCalled(); expect(engine.start).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review Engine installation' }));
    const dialog = await screen.findByRole('dialog', { name: 'Review Engine installation' });
    expect(dialog.querySelector('[data-journey-state="textanalysis.engine_review"]')).toBeTruthy();
    expect(within(dialog).getByText('Setup choice: Preview.')).toBeTruthy();
    expect(within(dialog).getByText(/shared Engine code only/)).toBeTruthy();
    expect(engine.start).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Install reviewed Engine' }));
    await waitFor(() => expect(engine.start).toHaveBeenCalledExactlyOnceWith(engine.quote.review_digest, expect.any(AbortSignal)));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(await screen.findByText('Windows is installing the Engine.')).toBeTruthy();
    expect(x.api.prepare).not.toHaveBeenCalled(); expect(x.api.confirm).not.toHaveBeenCalled(); expect(x.api.retain).not.toHaveBeenCalled();
    expect(x.dependencies.command).not.toHaveBeenCalled();
    await openSaved();
    expect(engine.start).toHaveBeenCalledOnce(); expect(x.api.prepare).not.toHaveBeenCalled(); expect(x.api.confirm).not.toHaveBeenCalled();
  });

  it('keeps saved preview evidence readable when Engine setup cannot be checked', async () => {
    const engine = engineFixture(); engine.status.mockRejectedValue(new Error('unavailable'));
    const x = fixture(false, engine); render(x.view());
    await screen.findByText(/Engine setup could not be verified/); await openSaved();
    expect(engine.review).not.toHaveBeenCalled(); expect(engine.start).not.toHaveBeenCalled();
    expect(x.api.prepare).not.toHaveBeenCalled(); expect(x.api.confirm).not.toHaveBeenCalled(); expect(x.dependencies.command).not.toHaveBeenCalled();
  });

  it.each(['account', 'same_account_session', 'role'] as const)('discards a late Engine review when %s changes without cancelling device work', async (change) => {
    const engine = engineFixture(), review = deferred<Awaited<ReturnType<EngineAcquisitionApi['review']>>>();
    engine.review.mockReturnValue(review.promise);
    const x = fixture(false, engine), rendered = render(x.view());
    await screen.findByText('Engine installation has not been requested.');
    fireEvent.click(screen.getByRole('button', { name: 'Review Engine installation' }));
    await waitFor(() => expect(engine.review).toHaveBeenCalledOnce());
    const signal = engine.review.mock.calls[0][0];
    act(() => {
      if (change === 'account') connect(x.transport, 'other-account', '2');
      if (change === 'same_account_session') connect(x.transport, ACCOUNT, '2');
      if (change === 'role') useUserStore.getState().actions.setUserRole('operator');
    });
    if (change === 'account') rendered.rerender(x.view('other-account'));
    await act(async () => review.resolve(engine.quote));
    expect(signal?.aborted).toBe(true); expect(screen.queryByRole('dialog')).toBeNull();
    expect(engine.start).not.toHaveBeenCalled(); expect(engine.cancel).not.toHaveBeenCalled();
    expect(x.api.prepare).not.toHaveBeenCalled(); expect(x.api.confirm).not.toHaveBeenCalled(); expect(x.dependencies.command).not.toHaveBeenCalled();
  });

  it('prepares an explicitly reviewed original scope before confirmation and keeps pending work read-only', async () => {
    const x = fixture(); render(x.view()); await selectProfile();
    expect(x.api.retain).not.toHaveBeenCalled(); expect(x.api.prepare).not.toHaveBeenCalled(); expect(x.api.confirm).not.toHaveBeenCalled();
    expect(x.dependencies.command).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Prepare preview scope' }));
    await screen.findByText('Original preview: ready for confirmation.');
    expect(screen.getByText('Frozen sample: 1 messages, up to 32, received and sent.')).toBeTruthy();
    expect(screen.getByText(/Retention window: after/)).toBeTruthy();
    expect(screen.getByText('Supported language: English. Tasks: Topics, Exact mentions.')).toBeTruthy();
    expect(screen.getByText(/Preparation has not started inference/)).toBeTruthy();
    expect(document.querySelector('[data-journey-state="textanalysis.preview_scope"]')).toBeTruthy();
    expect(x.api.prepare).toHaveBeenCalledOnce(); expect(x.api.confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm original preview' }));
    await screen.findByText('Original preview: pending.');
    expect(x.api.confirm).toHaveBeenCalledExactlyOnceWith(KEY, outcomeExample.confirmation, expect.any(AbortSignal));
    expect(x.api.retain.mock.calls.map(([intent]) => intent.kind)).toEqual(['preview_prepare', 'preview_confirm']);
    expect(document.querySelector('[data-journey-state="textanalysis.preview_pending"]')).toBeTruthy();
    fireEvent.click(screen.getAllByRole('button', { name: 'Check original preview' })[0]);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Check retained previews' }) as HTMLButtonElement).disabled).toBe(false));
    expect(x.api.prepare).toHaveBeenCalledOnce(); expect(x.api.confirm).toHaveBeenCalledOnce();
    expect(x.dependencies.command).not.toHaveBeenCalled();
  });

  it('recovers a confirmable original request without executing it until exact retry is chosen', async () => {
    const x = fixture(), intent = clientIntent.parse(confirmIntentExample);
    x.journal.set(`${intent.kind}:${intent.key}`, await retained(intent));
    x.setOutcome(await verifyPreviewOutcome(clone(outcomeExample), KEY));
    render(x.view());
    const retry = await screen.findByRole('button', { name: 'Retry exact preview request' });
    expect(x.api.confirm).not.toHaveBeenCalled(); expect(x.api.retain).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Check original preview' }));
    await screen.findByText('Original preview: ready for confirmation.');
    expect((screen.getByRole('button', { name: 'Confirm original preview' }) as HTMLButtonElement).disabled).toBe(true);
    expect(x.api.confirm).not.toHaveBeenCalled();
    fireEvent.click(retry);
    await screen.findByText('Original preview: pending.');
    expect(x.api.confirm).toHaveBeenCalledExactlyOnceWith(KEY, outcomeExample.confirmation, expect.any(AbortSignal));
    expect(x.api.prepare).not.toHaveBeenCalled();
  });

  it('preserves an unknown prepare key across refresh and recovers its result without automatic replay', async () => {
    const x = fixture();
    x.api.prepare.mockImplementationOnce(async () => {
      x.setOutcome(await verifyPreviewOutcome(clone(outcomeExample), KEY)); throw new TextAnalysisApiError('unknown');
    });
    render(x.view()); await selectProfile(); fireEvent.click(screen.getByRole('button', { name: 'Prepare preview scope' }));
    await screen.findByText(/The preview outcome is unknown/);
    const original = clone(x.api.retain.mock.calls[0][0]);
    fireEvent.click(screen.getByRole('button', { name: 'Check retained previews' }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Check original preview' }) as HTMLButtonElement).disabled).toBe(false));
    expect(x.api.prepare).toHaveBeenCalledOnce(); expect(x.api.retain).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Check original preview' }));
    await screen.findByText('Original preview: ready for confirmation.');
    expect(x.api.retain.mock.calls[0][0]).toEqual(original);
    expect(x.api.prepare).toHaveBeenCalledOnce(); expect(x.api.confirm).not.toHaveBeenCalled();
  });

  it.each([
    ['empty', 'The complete assessment produced no showable output; the preview was not spent.'],
    ['incomplete', 'Examination was incomplete and produced no showable output; the preview was not spent.'],
    ['overflow', 'The selected output exceeded its limit; no output was retained and the preview was not spent.'],
    ['result_deleted', 'The saved result was erased. Deletion does not replenish a spent preview.'],
  ] as const)('shows the original %s terminal result without preparing a replacement', async (reason, message) => {
    const x = fixture(), intent = clientIntent.parse(prepareIntentExample);
    x.journal.set(`${intent.kind}:${intent.key}`, await retained(intent));
    const outcome = await verifyPreviewOutcome(clone(outcomeExample), KEY);
    if (reason === 'result_deleted') {
      outcome.receipt.state = 'result_deleted'; outcome.scope = null; outcome.confirmation = null; outcome.disclosure = null; outcome.detailed_scope_available = false;
    } else { outcome.receipt.state = 'failed'; outcome.receipt.terminal_reason = reason; }
    x.setOutcome(outcome); render(x.view());
    fireEvent.click(await screen.findByRole('button', { name: 'Check original preview' }));
    expect(await screen.findByText(message)).toBeTruthy();
    expect(document.querySelector('[data-journey-state="textanalysis.preview_terminal"]')).toBeTruthy();
    expect(x.api.prepare).not.toHaveBeenCalled(); expect(x.api.confirm).not.toHaveBeenCalled(); expect(x.api.retain).not.toHaveBeenCalled();
  });

  it('reads saved full output without models or module availability and keeps occurrence units distinct', async () => {
    const x = fixture(); x.dependencies.get.mockRejectedValue(new Error('Unavailable'));
    render(x.view()); const result = within(await openSaved());
    const topic = result.getByRole('region', { name: 'Preview topic winner' });
    const mention = result.getByRole('region', { name: 'Preview mention winner' });
    expect(within(topic).getByText('Topics: topic.synthetic')).toBeTruthy();
    expect(within(mention).getByText('Exact mentions: concept.synthetic')).toBeTruthy();
    expect(within(mention).getByText('1 supporting conversations · 1 distinct messages · 2 exact occurrences.')).toBeTruthy();
    expect(within(topic).getByText('+0 additional topic categories in the fully assessed frozen scope.')).toBeTruthy();
    expect(within(mention).getByText('+0 additional mention categories in the fully assessed frozen scope.')).toBeTruthy();
    expect(result.getAllByText('Display label unavailable; the original category ID is shown.')).toHaveLength(2);
    expect(result.getAllByText(/Eligible: 1 · Completely assessed: 1 · Partial: 0/)).toHaveLength(2);
    expect(result.getByText(/Topics and literal mentions do not establish requests/)).toBeTruthy();
    expect(document.querySelector('[data-journey-state="textanalysis.preview_results"]')).toBeTruthy();
    expect(x.api.prepare).not.toHaveBeenCalled(); expect(x.api.confirm).not.toHaveBeenCalled(); expect(x.api.command).not.toHaveBeenCalled();
    expect(x.dependencies.command).not.toHaveBeenCalled();
    const copy = result.getByText('Topics: topic.synthetic').closest('[aria-label="Verified shown preview"]')?.textContent;
    expect(copy).not.toContain(KEY); expect(copy).not.toContain(savedExample.output.contribution_id);
    expect(copy).not.toContain(savedExample.output.spec_sha256); expect(copy).not.toContain('compact_jws');
  });

  it('qualifies partial mention support and omits only its unresolved additional-category total', async () => {
    const x = fixture(true); render(x.view()); const result = within(await openSaved());
    const mention = within(result.getByRole('region', { name: 'Preview mention winner' }));
    expect(mention.getByText('1 supporting conversations · 1 distinct messages · 1 exact occurrences.')).toBeTruthy();
    expect(mention.getByText('Complete / partial supporting messages: 0 / 1.')).toBeTruthy();
    expect(mention.getByText('Ranked by observed support in the assessed portion; the full ranking is unavailable.')).toBeTruthy();
    expect(mention.getByText('Share of completely assessed messages: Unavailable. This is support, not model accuracy.')).toBeTruthy();
    expect(mention.queryByText(/additional mention categories/)).toBeNull();
    expect(result.getByText('+0 additional topic categories in the fully assessed frozen scope.')).toBeTruthy();
    expect(result.getByText(/Eligible: 1 · Completely assessed: 0 · Partial: 1/)).toBeTruthy();
  });

  it('opens each repeated Unicode occurrence with its exact source reference and UTF16 highlight', async () => {
    const x = fixture(); render(x.view()); const result = within(await openSaved());
    const mention = within(result.getByRole('region', { name: 'Preview mention winner' }));
    fireEvent.click(mention.getByRole('button', { name: /Open occurrence evidence 1/ }));
    let dialog = await screen.findByRole('dialog', { name: 'Finding source' });
    await waitFor(() => expect(dialog.querySelector('mark')?.textContent).toBe('echo'));
    expect(dialog.querySelector('mark')?.previousSibling?.textContent).toBe('😀 ');
    expect(x.api.evidence.mock.calls[0][0].span).toEqual({ start: 2, end: 6 });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(mention.getByRole('button', { name: /Open occurrence evidence 2/ }));
    dialog = await screen.findByRole('dialog', { name: 'Finding source' });
    await waitFor(() => expect(dialog.querySelector('mark')?.textContent).toBe('echo'));
    expect(dialog.querySelector('mark')?.previousSibling?.textContent).toBe('😀 echo ');
    expect(x.api.evidence.mock.calls[1][0].span).toEqual({ start: 7, end: 11 });
    expect(x.api.evidence.mock.calls[0][0].message_ref).toBe(x.api.evidence.mock.calls[1][0].message_ref);
  });

  it.each(['account', 'same_account_session', 'role'] as const)('clears saved output and evidence when %s changes', async (change) => {
    const x = fixture(), rendered = render(x.view()); const result = within(await openSaved());
    fireEvent.click(result.getByRole('button', { name: /Open message evidence 1/ }));
    await screen.findByRole('dialog', { name: 'Finding source' });
    act(() => {
      if (change === 'account') connect(x.transport, 'other-account', '2');
      if (change === 'same_account_session') connect(x.transport, ACCOUNT, '2');
      if (change === 'role') useUserStore.getState().actions.setUserRole('operator');
    });
    if (change === 'account') rendered.rerender(x.view('other-account'));
    await waitFor(() => expect(screen.queryByLabelText('Verified shown preview')).toBeNull());
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Finding source' })).toBeNull());
    expect(x.api.prepare).not.toHaveBeenCalled(); expect(x.api.confirm).not.toHaveBeenCalled();
  });

  it('discards a late saved result after the same account receives a new session', async () => {
    const x = fixture(), saved = deferred<Awaited<ReturnType<TextAnalysisApi['saved']>>>();
    x.api.saved.mockReturnValue(saved.promise); render(x.view());
    fireEvent.click(await screen.findByRole('button', { name: 'Open saved preview 1' }));
    await waitFor(() => expect(x.api.saved).toHaveBeenCalledOnce());
    const signal = x.api.saved.mock.calls[0][1];
    act(() => connect(x.transport, ACCOUNT, '2'));
    await act(async () => saved.resolve(await parseSavedPreview(clone(savedExample), savedExample.output.contribution_id)));
    expect(signal?.aborted).toBe(true); expect(screen.queryByLabelText('Verified shown preview')).toBeNull();
  });
});
