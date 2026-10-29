import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NlpDependencyControls } from '../src/components/NlpDependencyControls';
import { TextAnalysisExecutionControls } from '../src/components/TextAnalysisExecutionControls';
import type { EngineAcquisitionApi } from '../src/api/engineAcquisition';
import type { NlpDependenciesApi, NlpDependencyCommand, NlpDependencyObservation, NlpDependencyPlan, NlpDependencyResult, NlpDependencyStatus } from '../src/services/nlpDependenciesApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

vi.mock('../src/components/TextAnalysisExecutionControls', () => ({ TextAnalysisExecutionControls: vi.fn(() => null) }));

const profile = { profile_id: 'topics-and-mentions', revision: 'one', profile_sha256: 'a'.repeat(64), catalogued: true, installed: false, prepared: false, withdrawn: false, storage_state: 'absent', languages: ['en'], tasks: ['message_topics', 'concept_mentions'], resources: { download_bytes: 2048, installed_bytes: 4096, rollback_bytes: 2048, temporary_bytes: 4096, transition_peak_memory_bytes: 8192, inference_threads: 2 } };
const status: NlpDependencyStatus = { configured: true, profiles: [profile], available_profiles: [], acquisitions: [], selected: null, failure: null, resume_granularity: 'verified_files_current_file_restarts' };
const unavailable: NlpDependencyStatus = { ...status, configured: false, profiles: [], failure: 'nlp_unavailable' };
const quote: NlpDependencyPlan = { operation_id: 'a'.repeat(32), profile_sha256: profile.profile_sha256, declared_full_artifact_download_bytes: 2048, data_download_bytes: 1024, incremental_data_download_bytes: 512, profile_metadata_bytes: 100, installed_bytes: 4096, rollback_bytes: 2048, acquisition_temporary_bytes: 1152, transition_peak_memory_bytes: 8192, inference_threads: 2 };
const observation: NlpDependencyObservation = { id: 'b'.repeat(32), kind: 'acquire', profile_sha256: profile.profile_sha256, profile_id: profile.profile_id, revision: profile.revision, state: 'running', in_flight: true, verified_files: 2, resume_granularity: status.resume_granularity };
function fixture(initial = status) {
  let current = observation;
  return {
    get: vi.fn().mockResolvedValue(initial), observe: vi.fn(async () => current),
    command: vi.fn(async (input: NlpDependencyCommand, _signal?: AbortSignal): Promise<NlpDependencyResult> => {
      if (input.action === 'pause') current = { ...current, state: 'paused', in_flight: false };
      if (input.action === 'resume') current = { ...current, state: 'running', in_flight: true };
      if (input.action === 'cancel') current = { ...current, state: 'cancelled', in_flight: false, verified_files: 0 };
      return { status: initial, operation: input.action === 'plan' ? quote : input.action === 'acquire' ? observation.id : null };
    }),
  };
}
function view(api: NlpDependenciesApi, account = 'account-one', engineApi?: EngineAcquisitionApi) {
  return <ThemeProvider theme={theme}><NlpDependencyControls api={api} engineApi={engineApi} creatorAccountId={account} /></ThemeProvider>;
}
beforeEach(() => { useUserStore.getState().actions.setUserRole('creator-ceo'); bridgeTransportStore.bindAccount('account-one'); });
afterEach(() => { cleanup(); bridgeTransportStore.reset(); useUserStore.getState().actions.setUserRole(null); });
async function review() { fireEvent.click(await screen.findByRole('button', { name: 'Review download for topics-and-mentions' })); await screen.findByRole('dialog', { name: 'Review download costs' }); }
function feedbackRegion() {
  const node = document.querySelector('[data-reserved-region="dependency-feedback"]');
  if (!(node instanceof HTMLElement)) throw new Error('Dependency feedback is missing');
  return node;
}
async function readFeedback(text: string | RegExp) {
  fireEvent.click(within(feedbackRegion()).getByRole('button', { name: 'Show details' }));
  const dialog = await screen.findByRole('dialog', { name: 'Details' });
  expect(within(dialog).getByText(text)).toBeTruthy();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Details' })).toBeNull());
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
function connect(suffix = '1') {
  bridgeTransportStore.acceptSession({ connection_id: `10000000-0000-4000-8000-00000000000${suffix}`,
    bridge_session_id: `20000000-0000-4000-8000-00000000000${suffix}`, creator_account_id: 'account-one',
    negotiated_protocol_version: '2', server_version: 'example' });
}
describe('NlpDependencyControls', () => {
  it('reserves compact feedback from loading through combined failure and recovery', async () => {
    const api = fixture(), initial = deferred<NlpDependencyStatus>(), refreshed = deferred<NlpDependencyStatus>();
    api.get.mockReturnValueOnce(initial.promise);
    render(view(api));
    const region = feedbackRegion(), wrapper = region.parentElement, classes = region.className;
    expect(within(region).getByRole('status').textContent).toBe('Checking…');
    expect(document.querySelector('[data-journey-state="textanalysis.unavailable"]')).toBeNull();
    await readFeedback('Checking text analysis availability…');
    await act(async () => initial.reject(new Error('untrusted failure')));
    expect(feedbackRegion()).toBe(region);
    expect(feedbackRegion().parentElement).toBe(wrapper);
    expect(within(region).getByRole('alert').textContent).toBe('Needs attention');
    expect(document.querySelector('[data-journey-state="textanalysis.unavailable"]')).toBe(wrapper);
    await readFeedback('Text analysis settings could not be updated. Your existing selection may still be active. Check status before trying again. Text analysis is unavailable until a qualified profile and compatible installed engine are available.');
    expect(screen.queryByText('untrusted failure')).toBeNull();
    api.get.mockReturnValueOnce(refreshed.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Check status' }));
    expect(document.querySelector('[data-journey-state="textanalysis.unavailable"]')).toBeNull();
    await readFeedback('Updating text analysis settings… Checking text analysis availability…');
    await act(async () => refreshed.resolve(status));
    await screen.findByRole('button', { name: 'Review download for topics-and-mentions' });
    expect(feedbackRegion()).toBe(region);
    expect(feedbackRegion().parentElement).toBe(wrapper);
    expect(feedbackRegion().className).toBe(classes);
    expect(document.querySelectorAll('[data-reserved-region="dependency-feedback"]')).toHaveLength(1);
    expect(within(region).queryByRole('button', { name: 'Show details' })).toBeNull();
    expect(api.command).not.toHaveBeenCalled();
  });
  it.each(['disconnect', 'session'] as const)('aborts a pending review and discards its late result after %s', async (change) => {
    connect();
    const api = fixture(), pending = deferred<NlpDependencyResult>();
    api.command.mockReturnValueOnce(pending.promise);
    render(view(api));
    fireEvent.click(await screen.findByRole('button', { name: 'Review download for topics-and-mentions' }));
    const signal = api.command.mock.calls[0][1];
    api.get.mockResolvedValue(unavailable);
    act(() => { if (change === 'disconnect') bridgeTransportStore.markDisconnected(); else connect('2'); });
    await act(async () => pending.resolve({ operation: quote, status }));
    expect(signal?.aborted).toBe(true);
    expect(screen.queryByRole('dialog', { name: 'Review download costs' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review download for topics-and-mentions' })).toBeNull();
    await readFeedback(/Text analysis is unavailable until/);
    expect(api.command).toHaveBeenCalledTimes(1);
  });
  it('clears expanded failure feedback when the connection changes', async () => {
    connect();
    const api = fixture(); api.get.mockRejectedValueOnce(new Error('untrusted failure'));
    render(view(api));
    await waitFor(() => expect(within(feedbackRegion()).getByRole('alert').textContent).toBe('Needs attention'));
    fireEvent.click(within(feedbackRegion()).getByRole('button', { name: 'Show details' }));
    const dialog = await screen.findByRole('dialog', { name: 'Details' });
    expect(dialog.textContent).toContain('Text analysis settings could not be updated.');
    api.get.mockResolvedValue(unavailable);
    act(() => bridgeTransportStore.markDisconnected());
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Details' })).toBeNull());
    await readFeedback('Text analysis is unavailable until a qualified profile and compatible installed engine are available.');
    expect(api.command).not.toHaveBeenCalled();
  });
  it('keeps shared Engine review separate when no qualified profile service is configured', async () => {
    const api = fixture(unavailable);
    const idle = { operation_id: null, state: 'not_requested' as const, installed: false, in_flight: false,
      cancel_requested: false, deadline_reached: false, progress_percent: null, reason: null, hresult: null };
    const engineApi = { selection: 'module' as const, supported: () => true, status: vi.fn().mockResolvedValue(idle),
      review: vi.fn().mockResolvedValue({ availability: 'unavailable', reason: 'optional_quote_unavailable' }),
      start: vi.fn(), cancel: vi.fn() };
    render(view(api, 'account-one', engineApi));
    expect(await screen.findByText('Engine installation has not been requested.')).toBeTruthy();
    await waitFor(() => expect(document.querySelector('[data-journey-state="textanalysis.unavailable"]')).toBeTruthy());
    await readFeedback(/Text analysis is unavailable until a qualified profile/);
    fireEvent.click(screen.getByRole('button', { name: 'Review Engine installation' }));
    expect(await screen.findByText(/A verified Engine installation quote is unavailable/)).toBeTruthy();
    expect(engineApi.review).toHaveBeenCalledOnce(); expect(engineApi.start).not.toHaveBeenCalled();
    expect(api.command).not.toHaveBeenCalled();
  });

  it('uses fresh plan metadata when the cached profile has no support or resource metadata', async () => {
    const cached = { ...profile, languages: null, tasks: null, resources: null };
    const api = fixture({ ...status, profiles: [cached] });
    api.command.mockResolvedValueOnce({ operation: quote, status });
    render(view(api));
    expect(await screen.findByText('Language and task support: not yet verified.')).toBeTruthy();
    await review();
    const dialog = within(screen.getByRole('dialog', { name: 'Review download costs' }));
    expect(dialog.getByText('Supported languages: en.')).toBeTruthy();
    expect(dialog.getByText('Supported tasks: Message topics, Exact mentions.')).toBeTruthy();
    expect((dialog.getByRole('button', { name: 'Download reviewed files' }) as HTMLButtonElement).disabled).toBe(false);
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan']);
  });
  it('discloses every supported language and task from the matching response profile', async () => {
    const fresh = { ...profile, languages: ['en', 'fi', 'sv'] };
    const api = fixture(); api.command.mockResolvedValueOnce({ operation: quote, status: { ...status, profiles: [fresh] } });
    render(view(api)); await review();
    const dialog = within(screen.getByRole('dialog', { name: 'Review download costs' }));
    expect(dialog.getByText('Supported languages: en, fi, sv.')).toBeTruthy();
    expect(dialog.getByText('Supported tasks: Message topics, Exact mentions.')).toBeTruthy();
    expect(dialog.getByText(/Review Engine installation separately/)).toBeTruthy();
    expect(dialog.getByText(/Profile-declared complete download budget:/)).toBeTruthy();
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan']);
  });
  it.each([
    ['message_topics', 'Message topics'],
    ['concept_mentions', 'Exact mentions'],
  ])('accepts a fresh profile supporting only %s', async (task, label) => {
    const fresh = { ...profile, tasks: [task] };
    const api = fixture(); api.command.mockResolvedValueOnce({ operation: quote, status: { ...status, profiles: [fresh] } });
    render(view(api)); await review();
    const dialog = within(screen.getByRole('dialog', { name: 'Review download costs' }));
    expect(dialog.getByText(`Supported tasks: ${label}.`)).toBeTruthy();
    expect(dialog.getByText('Supported languages: en.')).toBeTruthy();
    fireEvent.click(dialog.getByRole('button', { name: 'Download reviewed files' }));
    await waitFor(() => expect(api.command).toHaveBeenCalledWith({ action: 'acquire', profile_id: profile.profile_id, revision: profile.revision, operation_id: quote.operation_id }, expect.any(AbortSignal)));
  });
  it.each<[string, NlpDependencyStatus['profiles']]>([
    ['missing profile', []],
    ['changed digest', [{ ...profile, profile_sha256: 'c'.repeat(64) }]],
    ['changed identity', [{ ...profile, revision: 'two' }]],
    ['duplicate identity', [profile, profile]],
    ['conflicting digest identity', [profile, { ...profile, revision: 'two' }]],
    ['missing languages', [{ ...profile, languages: null }]],
    ['missing tasks', [{ ...profile, tasks: null }]],
    ['empty tasks', [{ ...profile, tasks: [] }]],
    ['duplicate tasks', [{ ...profile, tasks: ['message_topics', 'message_topics'] }]],
    ['unknown task', [{ ...profile, tasks: ['message_topics', 'unknown'] }]],
    ['missing resources', [{ ...profile, resources: null }]],
    ['mismatched resources', [{ ...profile, resources: { ...profile.resources, installed_bytes: 4097 } }]],
    ['withdrawn profile', [{ ...profile, withdrawn: true }]],
    ['uncatalogued profile', [{ ...profile, catalogued: false }]],
  ])('refuses download review with %s in the fresh response', async (_name, profiles) => {
    const api = fixture(); api.command.mockResolvedValueOnce({ operation: quote, status: { ...status, profiles: [...profiles] } });
    render(view(api));
    fireEvent.click(await screen.findByRole('button', { name: 'Review download for topics-and-mentions' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check status' }).hasAttribute('disabled')).toBe(false));
    await readFeedback(/Text analysis settings could not be updated/);
    expect(screen.queryByRole('dialog', { name: 'Review download costs' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Download reviewed files' })).toBeNull();
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan']);
  });
  it('does not claim verified language or task support from unapproved or incomplete cached metadata', async () => {
    const api = fixture({ ...status, profiles: [{ ...profile, catalogued: false }, { ...profile, profile_sha256: 'd'.repeat(64), resources: null }] });
    render(view(api));
    expect(await screen.findAllByText('Language and task support: not yet verified.')).toHaveLength(2);
    expect(screen.queryByText(/^Supported languages:/)).toBeNull();
    expect(screen.queryByText(/^Supported tasks:/)).toBeNull();
    expect(api.command).not.toHaveBeenCalled();
  });
  it.each([
    { ...quote, profile_sha256: 'c'.repeat(64) },
    { ...quote, incremental_data_download_bytes: quote.data_download_bytes + 1 },
    { ...quote, data_download_bytes: quote.declared_full_artifact_download_bytes + 1 },
    { ...quote, acquisition_temporary_bytes: profile.resources.temporary_bytes + 1 },
    { ...quote, profile_metadata_bytes: 0 },
  ])('refuses an incoherent returned plan %#', async (operation) => {
    const api = fixture(); api.command.mockResolvedValueOnce({ operation, status });
    render(view(api)); fireEvent.click(await screen.findByRole('button', { name: 'Review download for topics-and-mentions' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check status' }).hasAttribute('disabled')).toBe(false));
    await readFeedback(/Text analysis settings could not be updated/);
    expect(screen.queryByRole('dialog', { name: 'Review download costs' })).toBeNull();
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan']);
  });
  it('disables an open review if a later status withdraws its profile', async () => {
    const api = fixture(); render(view(api)); await review();
    api.get.mockResolvedValue({ ...status, profiles: [{ ...profile, withdrawn: true }] });
    fireEvent.click(screen.getByText('Check status'));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Download reviewed files' }) as HTMLButtonElement).disabled).toBe(true));
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan']);
  });
  it('preserves the unavailable baseline without automatic setup actions', async () => {
    const api = fixture(unavailable); render(view(api));
    await waitFor(() => expect(document.querySelector('[data-journey-state="textanalysis.unavailable"]')).toBeTruthy());
    await readFeedback(/Text analysis is unavailable until/);
    expect(screen.getByText(/other analytics remain available/)).toBeTruthy();
    expect(api.get).toHaveBeenCalledTimes(1); expect(api.command).not.toHaveBeenCalled(); expect(api.observe).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: 'Check status' }));
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(api.command).not.toHaveBeenCalled();
  });
  it('requires exact cost review before acquiring files', async () => {
    const api = fixture(); render(view(api)); await review();
    for (const [label, amount] of [['Download now', 512], ['Installed size', 4096], ['Temporary disk space', 1152], ['Rollback space', 2048], ['Peak memory during transition', 8192]] as const) expect(screen.getByText(`${label}: ${new Intl.NumberFormat().format(amount)} bytes`, { normalizer: (text) => text })).toBeTruthy();
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan']);
    fireEvent.click(screen.getByRole('button', { name: 'Download reviewed files' }));
    await waitFor(() => expect(api.command).toHaveBeenCalledWith({ action: 'acquire', profile_id: profile.profile_id, revision: profile.revision, operation_id: quote.operation_id }, expect.any(AbortSignal)));
    expect(await screen.findByText(/2 verified files retained/)).toBeTruthy();
    expect(screen.getByText(/interrupted file restarts/)).toBeTruthy();
  });
  it('keeps download pause resume and cancellation distinct from processing controls', async () => {
    const api = fixture({ ...status, selected: { profile_id: profile.profile_id, revision: profile.revision, profile_sha256: profile.profile_sha256, mode: 'enabled', selection_epoch: 1, admission_epoch: 1 } });
    render(view(api));
    await screen.findByRole('button', { name: 'Review download for topics-and-mentions' });
    expect(screen.queryByRole('button', { name: 'Pause processing' })).toBeNull();
    await review(); fireEvent.click(screen.getByRole('button', { name: 'Download reviewed files' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Pause download' }));
    await waitFor(() => expect(screen.getByText(/Download paused/)).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Resume download' }));
    await waitFor(() => expect(screen.getByText(/Downloading/)).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel download' }));
    await waitFor(() => expect(screen.getByText(/Download cancelled/)).toBeTruthy());
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan', 'acquire', 'pause', 'resume', 'cancel']);
  });
  it('aborts and discards pending plan results when the account changes', async () => {
    const api = fixture(); let resolve!: (result: NlpDependencyResult) => void;
    api.command.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const rendered = render(view(api)); fireEvent.click(await screen.findByRole('button', { name: 'Review download for topics-and-mentions' }));
    const signal = api.command.mock.calls[0][1]; api.get.mockResolvedValue(unavailable);
    act(() => bridgeTransportStore.bindAccount('account-two')); rendered.rerender(view(api, 'account-two'));
    await act(async () => resolve({ operation: quote, status }));
    expect(signal?.aborted).toBe(true); expect(screen.queryByRole('dialog')).toBeNull();
    await readFeedback(/Text analysis is unavailable until/);
    expect(api.command).toHaveBeenCalledTimes(1);
  });
  it('does not observe or display an acquisition returned after an account switch', async () => {
    const api = fixture(); let resolve!: (result: NlpDependencyResult) => void;
    const rendered = render(view(api)); await review();
    api.command.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    fireEvent.click(screen.getByRole('button', { name: 'Download reviewed files' }));
    api.get.mockResolvedValue(unavailable); act(() => bridgeTransportStore.bindAccount('account-two')); rendered.rerender(view(api, 'account-two'));
    await act(async () => resolve({ operation: observation.id, status }));
    expect(api.observe).not.toHaveBeenCalled(); expect(screen.queryByRole('button', { name: 'Cancel download' })).toBeNull();
  });
  it('distinguishes installed files from prepared profiles and hides exception text', async () => {
    const api = fixture({ ...status, profiles: [{ ...profile, installed: true }] }); render(view(api));
    expect(await screen.findByText(/Files installed; preparation required/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Use topics-and-mentions' })).toBeNull();
    api.command.mockRejectedValueOnce(new Error('untrusted server secret'));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare topics-and-mentions' }));
    expect(await screen.findByRole('alert')).toBeTruthy(); expect(screen.queryByText(/untrusted server secret/)).toBeNull();
    await readFeedback(/Text analysis settings could not be updated/);
    expect(screen.getByText(/Files installed; preparation required/)).toBeTruthy();
    expect(api.command).toHaveBeenCalledWith({ action: 'prepare', profile_sha256: profile.profile_sha256 }, expect.any(AbortSignal));
  });
  it('rediscovers interrupted downloads without automatically resuming them', async () => {
    const paused = { ...observation, state: 'interrupted', in_flight: false };
    const api = fixture({ ...status, acquisitions: [paused] }); api.observe.mockResolvedValue(paused);
    render(view(api));
    expect(await screen.findByRole('button', { name: 'Resume download' })).toBeTruthy();
    expect(api.command).not.toHaveBeenCalled();
    expect(screen.getByText(/interrupted file restarts/)).toBeTruthy();
  });
  it('discloses range recovery and full-restart fallback without starting a download', async () => {
    const paused: NlpDependencyObservation = { ...observation, state: 'interrupted', in_flight: false, resume_granularity: 'verified_files_and_rehashed_prefix' };
    const api = fixture({ ...status, acquisitions: [paused], resume_granularity: 'verified_files_and_rehashed_prefix' });
    api.observe.mockResolvedValue(paused);
    render(view(api));
    expect(await screen.findByText(/Resume rechecks saved bytes/)).toBeTruthy();
    expect(screen.getByText(/Otherwise the interrupted file restarts/)).toBeTruthy();
    expect(api.command).not.toHaveBeenCalled();
  });
  it('recovers an accepted download after a lost command response through cached status', async () => {
    const api = fixture(); render(view(api)); await review();
    api.command.mockRejectedValueOnce(new Error('response lost'));
    fireEvent.click(screen.getByRole('button', { name: 'Download reviewed files' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check status' }).hasAttribute('disabled')).toBe(false));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Review download costs' })).toBeNull());
    await readFeedback(/Text analysis settings could not be updated/);
    api.get.mockResolvedValue({ ...status, acquisitions: [observation] });
    fireEvent.click(await screen.findByRole('button', { name: 'Check status' }));
    await waitFor(() => expect(api.observe).toHaveBeenCalledWith(observation.id, expect.any(AbortSignal)));
    expect(await screen.findByRole('button', { name: 'Pause download' })).toBeTruthy();
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan', 'acquire']);
  });
  it('does not infer durable processing state from cached dependency selection', async () => {
    const api = fixture({ ...unavailable, selected: { profile_id: profile.profile_id, revision: profile.revision, profile_sha256: profile.profile_sha256, mode: 'paused', selection_epoch: 1, admission_epoch: 1 } });
    render(view(api));
    await waitFor(() => expect(document.querySelector('[data-journey-state="textanalysis.unavailable"]')).toBeTruthy());
    await readFeedback(/Text analysis is unavailable until/);
    expect(screen.queryByText(/Saved selection:/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Enable processing' })).toBeNull();
    expect(api.command).not.toHaveBeenCalled();
  });
  it('prioritizes a running download over an older paused operation', async () => {
    const paused = { ...observation, id: 'a'.repeat(32), state: 'paused', in_flight: false };
    const api = fixture({ ...status, acquisitions: [paused, observation] }); render(view(api));
    await waitFor(() => expect(api.observe).toHaveBeenCalledWith(observation.id, expect.any(AbortSignal)));
    expect(api.observe).not.toHaveBeenCalledWith(paused.id, expect.anything());
    expect(await screen.findByRole('button', { name: 'Pause download' })).toBeTruthy();
  });
  it('delegates account processing to the durable controls', async () => {
    const api = fixture({ ...status, selected: { profile_id: profile.profile_id, revision: profile.revision, profile_sha256: profile.profile_sha256, mode: 'paused', selection_epoch: 1, admission_epoch: 1 } });
    render(view(api));
    await screen.findByRole('button', { name: 'Review download for topics-and-mentions' });
    expect(TextAnalysisExecutionControls).toHaveBeenCalledWith(expect.objectContaining({ creatorAccountId: 'account-one', dependenciesApi: api }), undefined);
    expect(screen.queryByRole('button', { name: 'Remove text analysis for this account' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Use previous profile' })).toBeNull();
    expect(api.command).not.toHaveBeenCalled();
  });
  it('refuses a preview Engine API in the module setup location without downloading a profile', () => {
    const api = fixture(unavailable);
    const engineApi = { selection: 'preview' as const, supported: vi.fn(() => true), status: vi.fn(), review: vi.fn(), start: vi.fn(), cancel: vi.fn() };
    render(view(api, 'account-one', engineApi));
    expect(screen.getByText(/Engine setup selection could not be verified/)).toBeTruthy();
    expect(engineApi.status).not.toHaveBeenCalled(); expect(engineApi.review).not.toHaveBeenCalled();
    expect(engineApi.start).not.toHaveBeenCalled(); expect(engineApi.cancel).not.toHaveBeenCalled(); expect(api.command).not.toHaveBeenCalled();
  });
  it('requires explicit preparation and shared file removal confirmation', async () => {
    const installed = { ...profile, installed: true };
    const prepared = { ...installed, prepared: true };
    const api = fixture({ ...status, profiles: [installed] }); render(view(api));
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare topics-and-mentions' }));
    await waitFor(() => expect(api.command).toHaveBeenCalledWith({ action: 'prepare', profile_sha256: profile.profile_sha256 }, expect.any(AbortSignal)));
    expect(api.command.mock.calls).toHaveLength(1);
    api.command.mockResolvedValueOnce({ operation: null, status: { ...status, profiles: [prepared] } });
    fireEvent.click(screen.getByRole('button', { name: 'Check available profiles' }));
    expect(screen.queryByRole('button', { name: 'Use topics-and-mentions' })).toBeNull();
    await waitFor(() => expect((screen.getByRole('button', { name: 'Remove topics-and-mentions' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Remove topics-and-mentions' }));
    expect(api.command.mock.calls.some(([input]) => input.action === 'remove')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }));
    await waitFor(() => expect(api.command.mock.calls.at(-1)?.[0]).toEqual({ action: 'remove', profile_id: profile.profile_id, revision: profile.revision }));
    expect(api.command.mock.calls.some(([input]) => ['select', 'account_enable', 'account_pause', 'rollback'].includes(input.action))).toBe(false);
  });
});
