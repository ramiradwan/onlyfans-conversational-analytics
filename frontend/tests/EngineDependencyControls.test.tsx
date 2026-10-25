import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EngineDependencyControls } from '../src/components/EngineDependencyControls';
import { EngineAcquisitionError, type EngineAcquisitionApi, type EngineStatus } from '../src/api/engineAcquisition';
import { theme } from '../src/theme';

const idle: EngineStatus = { operation_id: null, state: 'not_requested', installed: false, in_flight: false,
  cancel_requested: false, deadline_reached: false, progress_percent: null, reason: null, hresult: null };
const live: EngineStatus = { ...idle, operation_id: 'b'.repeat(32), state: 'acquiring', in_flight: true };
const quote = { availability: 'available' as const, review_digest: 'a'.repeat(64), engine_package_version: '1.0.42.0',
  expected_complete_package_bytes: 12345, declared_disk: { installed_space_budget_bytes: 4096,
    temporary_space_budget_bytes: 2048, rollback_space_budget_bytes: 0, required_free_space_bytes: 6144 } };
function fixture(initial = idle) {
  return { supported: vi.fn(() => true), status: vi.fn().mockResolvedValue(initial), review: vi.fn().mockResolvedValue(quote),
    start: vi.fn().mockResolvedValue(live), cancel: vi.fn().mockResolvedValue({ ...live, state: 'cancelling', cancel_requested: true }) };
}
function view(api: EngineAcquisitionApi, account: string | null = 'account-one') {
  return <ThemeProvider theme={theme}><EngineDependencyControls api={api} account={account} /></ThemeProvider>;
}
async function review() {
  fireEvent.click(await screen.findByRole('button', { name: 'Review Engine installation' }));
  return within(await screen.findByRole('dialog', { name: 'Review Engine installation' }));
}
const amount = (value: number) => `${new Intl.NumberFormat().format(value).replace(/\s+/gu, ' ')} bytes`;
async function reviewClosed() {
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Review Engine installation' })).toBeNull());
}
afterEach(() => { cleanup(); vi.useRealTimers(); });
describe('shared-device Engine controls', () => {
  it('mount and status checks are passive; review and confirmation are separate', async () => {
    const api = fixture(); render(view(api)); await screen.findByText('Engine installation has not been requested.');
    expect(api.status).toHaveBeenCalledOnce(); expect(api.review).not.toHaveBeenCalled(); expect(api.start).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Check Engine status' })); await waitFor(() => expect(api.status).toHaveBeenCalledTimes(2));
    const dialog = await review();
    expect(dialog.getByText('Engine version: 1.0.42.0')).toBeTruthy();
    expect(dialog.getByText(`Expected complete signed package: ${amount(12345)}`)).toBeTruthy();
    expect(dialog.getByText(`Declared installed space budget: ${amount(4096)}`)).toBeTruthy();
    expect(dialog.getByText(`Declared temporary space budget: ${amount(2048)}`)).toBeTruthy();
    expect(dialog.getByText(`Declared rollback space budget: ${amount(0)}`)).toBeTruthy();
    expect(dialog.getByText(`Declared required free space: ${amount(6144)}`)).toBeTruthy();
    expect(dialog.getByText(/Actual Windows transfer, cache reuse/)).toBeTruthy(); expect(api.start).not.toHaveBeenCalled();
    fireEvent.click(dialog.getByRole('button', { name: 'Install reviewed Engine' }));
    await waitFor(() => expect(api.start).toHaveBeenCalledWith(quote.review_digest, expect.any(AbortSignal)));
    await reviewClosed();
    expect(await screen.findByText('Windows is installing the Engine.')).toBeTruthy();
    expect(api.review).toHaveBeenCalledOnce(); expect(api.cancel).not.toHaveBeenCalled();
  });
  it('reconnects an existing operation and cancellation waits for settlement', async () => {
    const api = fixture(live); render(view(api)); await screen.findByText('Windows is installing the Engine.');
    expect((screen.getByRole('button', { name: 'Review Engine installation' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel Engine installation' }));
    await waitFor(() => expect(api.cancel).toHaveBeenCalledWith(live.operation_id, expect.any(AbortSignal)));
    expect(await screen.findByText(/Cancellation is requested; it is not proof/)).toBeTruthy();
    expect(api.start).not.toHaveBeenCalled(); expect(api.review).not.toHaveBeenCalled();
  });
  it('an unavailable quote cannot confirm an installation', async () => {
    const api = fixture(); api.review.mockResolvedValue({ availability: 'unavailable', reason: 'optional_quote_unavailable' });
    render(view(api)); await screen.findByText('Engine installation has not been requested.');
    fireEvent.click(screen.getByRole('button', { name: 'Review Engine installation' }));
    expect(await screen.findByText(/A verified Engine installation quote is unavailable/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Install reviewed Engine' })).toBeNull(); expect(api.start).not.toHaveBeenCalled();
  });
  it('changing account or unmounting discards a late review without cancelling the device operation', async () => {
    const api = fixture(); let resolve!: (value: typeof quote) => void;
    api.review.mockImplementation(() => new Promise((done) => { resolve = done; }));
    const rendered = render(view(api)); await screen.findByText('Engine installation has not been requested.');
    fireEvent.click(screen.getByRole('button', { name: 'Review Engine installation' }));
    const signal = api.review.mock.calls[0]?.[0] as AbortSignal;
    rendered.rerender(view(api, 'account-two'));
    await act(async () => { resolve(quote); });
    expect(signal.aborted).toBe(true); expect(screen.queryByRole('dialog')).toBeNull();
    rendered.unmount(); expect(api.cancel).not.toHaveBeenCalled(); expect(api.start).not.toHaveBeenCalled();
  });
  it('polling an existing operation remains passive across account changes', async () => {
    vi.useFakeTimers(); const api = fixture(live); const rendered = render(view(api));
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(api.status.mock.calls.length).toBeGreaterThan(1);
    const oldSignal = api.status.mock.calls.at(-1)?.[0] as AbortSignal;
    rendered.rerender(view(api, 'account-two')); await act(async () => { await Promise.resolve(); });
    expect(oldSignal.aborted).toBe(true);
    rendered.unmount(); const calls = api.status.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api.status).toHaveBeenCalledTimes(calls); expect(api.start).not.toHaveBeenCalled(); expect(api.cancel).not.toHaveBeenCalled();
  });
  it('recovery and servicing ownership fences never become a retry Start', async () => {
    const api = fixture({ ...idle, state: 'recovery_pending', operation_id: 'b'.repeat(32) });
    render(view(api)); await screen.findByText(/Engine installation needs attention/);
    expect((screen.getByRole('button', { name: 'Review Engine installation' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.review).not.toHaveBeenCalled(); cleanup();
    const blocked = fixture(); blocked.start.mockRejectedValue(new EngineAcquisitionError('ownership'));
    render(view(blocked)); await screen.findByText('Engine installation has not been requested.');
    const dialog = await review(); fireEvent.click(dialog.getByRole('button', { name: 'Install reviewed Engine' }));
    await screen.findByText(/Another installation request is unavailable/);
    await reviewClosed();
    blocked.status.mockResolvedValue({ ...idle, state: 'failed', operation_id: 'c'.repeat(32) });
    fireEvent.click(screen.getByRole('button', { name: 'Check Engine status' })); await waitFor(() => expect(blocked.status).toHaveBeenCalledTimes(2));
    expect((screen.getByRole('button', { name: 'Review Engine installation' }) as HTMLButtonElement).disabled).toBe(true);
    expect(blocked.start).toHaveBeenCalledOnce();
  });
  it('a stale review requires a fresh explicit review, with no automatic retry', async () => {
    const api = fixture(); api.start.mockRejectedValue(new EngineAcquisitionError('stale_review'));
    render(view(api)); await screen.findByText('Engine installation has not been requested.');
    const dialog = await review(); fireEvent.click(dialog.getByRole('button', { name: 'Install reviewed Engine' }));
    await screen.findByText(/Engine setup could not be verified/);
    await reviewClosed();
    expect(api.review).toHaveBeenCalledOnce(); expect(api.start).toHaveBeenCalledOnce(); expect(screen.queryByRole('dialog')).toBeNull();
  });
  it('a lost Start response is not cleared by an old terminal ticket', async () => {
    const old = { ...idle, state: 'failed' as const, operation_id: 'c'.repeat(32) };
    const api = fixture(old); api.start.mockRejectedValue(new Error('lost response'));
    render(view(api)); await screen.findByText('Engine installation did not complete.');
    const dialog = await review(); fireEvent.click(dialog.getByRole('button', { name: 'Install reviewed Engine' }));
    await screen.findByText(/Another installation request is unavailable/);
    await reviewClosed();
    fireEvent.click(screen.getByRole('button', { name: 'Check Engine status' })); await waitFor(() => expect(api.status).toHaveBeenCalledTimes(2));
    expect((screen.getByRole('button', { name: 'Review Engine installation' }) as HTMLButtonElement).disabled).toBe(true);
  });
  it('installed Engine does not enable model data or processing; unsupported browser stays passive', async () => {
    const api = fixture({ ...idle, state: 'installed', installed: true, operation_id: 'b'.repeat(32) });
    render(view(api)); expect(await screen.findByText('Engine installation verified.')).toBeTruthy();
    expect(screen.getByText(/Only Engine installation is verified/)).toBeTruthy(); expect(api.review).not.toHaveBeenCalled(); cleanup();
    const browser = fixture(); browser.supported.mockReturnValue(false); render(view(browser));
    expect(screen.getByText(/Windows desktop app/)).toBeTruthy(); expect(browser.status).not.toHaveBeenCalled(); expect(browser.start).not.toHaveBeenCalled();
  });
});
