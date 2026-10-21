import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NlpDependencyControls } from '../src/components/NlpDependencyControls';
import type { NlpDependenciesApi, NlpDependencyCommand, NlpDependencyObservation, NlpDependencyPlan, NlpDependencyResult, NlpDependencyStatus } from '../src/services/nlpDependenciesApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const profile = { profile_id: 'topics-and-mentions', revision: 'one', profile_sha256: 'a'.repeat(64), catalogued: true, installed: false, prepared: false, withdrawn: false, storage_state: 'absent', languages: ['en'], tasks: ['topics', 'mentions'], resources: null };
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
function view(api: NlpDependenciesApi, account = 'account-one') {
  return <ThemeProvider theme={theme}><NlpDependencyControls api={api} creatorAccountId={account} /></ThemeProvider>;
}
beforeEach(() => { useUserStore.getState().actions.setUserRole('creator-ceo'); bridgeTransportStore.bindAccount('account-one'); });
afterEach(() => { cleanup(); bridgeTransportStore.reset(); useUserStore.getState().actions.setUserRole(null); });
async function review() { fireEvent.click(await screen.findByRole('button', { name: 'Review download for topics-and-mentions' })); await screen.findByRole('dialog', { name: 'Review download costs' }); }
describe('NlpDependencyControls', () => {
  it('preserves the unavailable baseline without automatic setup actions', async () => {
    const api = fixture(unavailable); render(view(api));
    expect(await screen.findByText(/Text analysis is unavailable until/)).toBeTruthy();
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
    render(view(api)); fireEvent.click(await screen.findByRole('button', { name: 'Pause processing' }));
    await waitFor(() => expect(api.command.mock.calls[0][0]).toEqual({ action: 'account_pause' }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Review download for topics-and-mentions' }) as HTMLButtonElement).disabled).toBe(false));
    await review(); fireEvent.click(screen.getByRole('button', { name: 'Download reviewed files' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Pause download' }));
    await waitFor(() => expect(screen.getByText(/Download paused/)).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Resume download' }));
    await waitFor(() => expect(screen.getByText(/Downloading/)).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel download' }));
    await waitFor(() => expect(screen.getByText(/Download cancelled/)).toBeTruthy());
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['account_pause', 'plan', 'acquire', 'pause', 'resume', 'cancel']);
  });
  it('aborts and discards pending plan results when the account changes', async () => {
    const api = fixture(); let resolve!: (result: NlpDependencyResult) => void;
    api.command.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const rendered = render(view(api)); fireEvent.click(await screen.findByRole('button', { name: 'Review download for topics-and-mentions' }));
    const signal = api.command.mock.calls[0][1]; api.get.mockResolvedValue(unavailable);
    act(() => bridgeTransportStore.bindAccount('account-two')); rendered.rerender(view(api, 'account-two'));
    await act(async () => resolve({ operation: quote, status }));
    expect(signal?.aborted).toBe(true); expect(screen.queryByRole('dialog')).toBeNull();
    expect(await screen.findByText(/Text analysis is unavailable until/)).toBeTruthy();
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
    expect((screen.getByRole('button', { name: 'Use topics-and-mentions' }) as HTMLButtonElement).disabled).toBe(true);
    api.command.mockRejectedValueOnce(new Error('untrusted server secret'));
    fireEvent.click(screen.getByRole('button', { name: 'Prepare topics-and-mentions' }));
    expect(await screen.findByRole('alert')).toBeTruthy(); expect(screen.queryByText(/untrusted server secret/)).toBeNull();
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
  it('recovers an accepted download after a lost command response through cached status', async () => {
    const api = fixture(); render(view(api)); await review();
    api.command.mockRejectedValueOnce(new Error('response lost'));
    fireEvent.click(screen.getByRole('button', { name: 'Download reviewed files' }));
    expect(await screen.findByText(/Text analysis settings could not be updated/)).toBeTruthy();
    api.get.mockResolvedValue({ ...status, acquisitions: [observation] });
    fireEvent.click(await screen.findByRole('button', { name: 'Check status' }));
    await waitFor(() => expect(api.observe).toHaveBeenCalledWith(observation.id, expect.any(AbortSignal)));
    expect(await screen.findByRole('button', { name: 'Pause download' })).toBeTruthy();
    expect(api.command.mock.calls.map(([input]) => input.action)).toEqual(['plan', 'acquire']);
  });
  it('preserves the saved selection when the runtime is unavailable', async () => {
    const api = fixture({ ...unavailable, selected: { profile_id: profile.profile_id, revision: profile.revision, profile_sha256: profile.profile_sha256, mode: 'paused', selection_epoch: 1, admission_epoch: 1 } });
    render(view(api));
    expect(await screen.findByText(/Saved selection: topics-and-mentions/)).toBeTruthy();
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
  it('confirms account removal separately from shared profile cleanup', async () => {
    const api = fixture({ ...status, selected: { profile_id: profile.profile_id, revision: profile.revision, profile_sha256: profile.profile_sha256, mode: 'paused', selection_epoch: 1, admission_epoch: 1 } });
    render(view(api));
    fireEvent.click(await screen.findByRole('button', { name: 'Remove text analysis for this account' }));
    expect(api.command).not.toHaveBeenCalled();
    expect(await screen.findByText(/Text processing stops for this account/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Remove for this account' }));
    await waitFor(() => expect(api.command).toHaveBeenCalledWith({ action: 'remove' }, expect.any(AbortSignal)));
  });
  it('requires explicit preparation selection and removal confirmation', async () => {
    const installed = { ...profile, installed: true };
    const prepared = { ...installed, prepared: true };
    const api = fixture({ ...status, profiles: [installed] }); render(view(api));
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare topics-and-mentions' }));
    await waitFor(() => expect(api.command).toHaveBeenCalledWith({ action: 'prepare', profile_sha256: profile.profile_sha256 }, expect.any(AbortSignal)));
    expect(api.command.mock.calls).toHaveLength(1);
    api.command.mockResolvedValueOnce({ operation: null, status: { ...status, profiles: [prepared] } });
    fireEvent.click(screen.getByRole('button', { name: 'Check available profiles' }));
    const use = await screen.findByRole('button', { name: 'Use topics-and-mentions' });
    await waitFor(() => expect((use as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(use);
    await waitFor(() => expect(api.command.mock.calls.at(-1)?.[0]).toEqual({ action: 'select', profile_id: profile.profile_id, revision: profile.revision }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Remove topics-and-mentions' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Remove topics-and-mentions' }));
    expect(api.command.mock.calls.some(([input]) => input.action === 'remove')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }));
    await waitFor(() => expect(api.command.mock.calls.at(-1)?.[0]).toEqual({ action: 'remove', profile_id: profile.profile_id, revision: profile.revision }));
  });
});
