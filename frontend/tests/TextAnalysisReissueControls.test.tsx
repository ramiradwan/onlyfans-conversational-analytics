import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StrictMode } from 'react';

import { TextAnalysisReissueControls } from '../src/components/TextAnalysisReissueControls';
import { TextAnalysisReissueApiError, type ReissueStatusView, type TextAnalysisReissueApi } from '../src/services/textAnalysisReissueApi';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const FLOW = '10000000-0000-4000-8000-000000000001';
const awaiting: ReissueStatusView = { current: { flow_id: FLOW, state: 'awaiting_authorization', navigation_available: true } };
const adopted: ReissueStatusView = { current: { flow_id: FLOW, state: 'adopted', navigation_available: false } };
function connect(transport: ReturnType<typeof createBridgeTransportStore>, account = 'account-1', suffix = '1') {
  transport.bindAccount(account);
  transport.acceptSession({ connection_id: `30000000-0000-4000-8000-00000000000${suffix}`,
    bridge_session_id: `40000000-0000-4000-8000-00000000000${suffix}`, creator_account_id: account,
    negotiated_protocol_version: '2', server_version: 'example' });
}
function fixture() {
  const transport = createBridgeTransportStore(); connect(transport);
  const api = {
    status: vi.fn<TextAnalysisReissueApi['status']>().mockResolvedValue({ current: null }),
    begin: vi.fn<TextAnalysisReissueApi['begin']>().mockResolvedValue(awaiting),
    resume: vi.fn<TextAnalysisReissueApi['resume']>().mockResolvedValue(adopted),
    openBrowser: vi.fn<TextAnalysisReissueApi['openBrowser']>().mockResolvedValue(undefined),
  } satisfies TextAnalysisReissueApi;
  const view = (account = 'account-1') => <ThemeProvider theme={theme}><TextAnalysisReissueControls creatorAccountId={account} api={api} transport={transport} /></ThemeProvider>;
  return { api, transport, view };
}
beforeEach(() => useUserStore.getState().actions.setUserRole('creator-ceo'));
afterEach(() => { cleanup(); useUserStore.setState({ role: null, roleResolved: false }); vi.restoreAllMocks(); });

describe('Licence recovery controls', () => {
  it('ignores the aborted first status request during strict effect remount', async () => {
    const x = fixture(); let reject!: (error: Error) => void;
    x.api.status.mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }));
    render(<StrictMode>{x.view()}</StrictMode>);
    await screen.findByRole('button', { name: 'Recover existing licence' });
    await act(async () => reject(new Error('aborted prior request')));
    expect(screen.getByRole('button', { name: 'Recover existing licence' }).hasAttribute('disabled')).toBe(false);
    expect(screen.queryByText('Recovery could not be verified. Check the saved status before continuing.')).toBeNull();
    expect(x.api.begin).not.toHaveBeenCalled();
  });

  it('recovers an existing licence through explicit browser and original-request actions', async () => {
    const x = fixture(); render(x.view());
    const begin = await screen.findByRole('button', { name: 'Recover existing licence' });
    expect(x.api.status).toHaveBeenCalledOnce();
    expect(x.api.begin).not.toHaveBeenCalled(); expect(x.api.resume).not.toHaveBeenCalled();
    fireEvent.click(begin);
    const browser = await screen.findByRole('button', { name: 'Continue recovery in browser' });
    expect(x.api.openBrowser).not.toHaveBeenCalled();
    fireEvent.click(browser);
    await waitFor(() => expect(x.api.openBrowser).toHaveBeenCalledExactlyOnceWith(FLOW, expect.any(AbortSignal)));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check original recovery request' }).hasAttribute('disabled')).toBe(false));
    expect(x.api.resume).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Check original recovery request' }));
    await screen.findByText('Access is saved on this computer. Processing availability is checked separately.');
    expect(x.api.resume).toHaveBeenCalledExactlyOnceWith(FLOW, expect.any(AbortSignal));
    expect(document.body.textContent).not.toContain(FLOW);
    expect(document.querySelector('[data-journey-state="textanalysis.licence_recovery"]')).toBeTruthy();
  });

  it('discovers the original request after remount without starting a replacement', async () => {
    const x = fixture(); x.api.status.mockResolvedValue(awaiting);
    const first = render(x.view());
    await screen.findByRole('button', { name: 'Continue recovery in browser' });
    first.unmount(); render(x.view());
    await screen.findByRole('button', { name: 'Continue recovery in browser' });
    expect(x.api.status).toHaveBeenCalledTimes(2);
    expect(x.api.begin).not.toHaveBeenCalled(); expect(x.api.resume).not.toHaveBeenCalled();
  });

  it('requires saved status after an unknown begin outcome', async () => {
    const x = fixture(); x.api.begin.mockRejectedValueOnce(new TextAnalysisReissueApiError('unknown'));
    render(x.view()); fireEvent.click(await screen.findByRole('button', { name: 'Recover existing licence' }));
    await screen.findByText('Recovery could not be verified. Check the saved status before continuing.');
    expect(screen.getByRole('button', { name: 'Recover existing licence' }).hasAttribute('disabled')).toBe(true);
    x.api.status.mockResolvedValueOnce(awaiting);
    fireEvent.click(screen.getByRole('button', { name: 'Check saved recovery status' }));
    await screen.findByRole('button', { name: 'Continue recovery in browser' });
    expect(x.api.begin).toHaveBeenCalledOnce(); expect(x.api.resume).not.toHaveBeenCalled();
  });

  it('requires an explicit original-outcome check before a fresh expired request', async () => {
    const x = fixture();
    const expired: ReissueStatusView = { current: { flow_id: FLOW, state: 'expired', navigation_available: false } };
    x.api.status.mockResolvedValue(expired); x.api.resume.mockResolvedValue(expired);
    render(x.view());
    const check = await screen.findByRole('button', { name: 'Check original recovery request' });
    expect(screen.queryByRole('button', { name: 'Start a new recovery request' })).toBeNull();
    fireEvent.click(check);
    fireEvent.click(await screen.findByRole('button', { name: 'Start a new recovery request' }));
    await screen.findByRole('button', { name: 'Continue recovery in browser' });
    expect(x.api.resume).toHaveBeenCalledExactlyOnceWith(FLOW, expect.any(AbortSignal));
    expect(x.api.begin).toHaveBeenCalledOnce();
  });

  it.each(['account', 'session', 'role', 'disconnect'] as const)('discards a late response after %s changes', async change => {
    const x = fixture(); let resolve!: (value: ReissueStatusView) => void;
    x.api.begin.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const mounted = render(x.view()); fireEvent.click(await screen.findByRole('button', { name: 'Recover existing licence' }));
    await waitFor(() => expect(x.api.begin).toHaveBeenCalledOnce());
    act(() => {
      if (change === 'account') connect(x.transport, 'account-2', '2');
      if (change === 'session') connect(x.transport, 'account-1', '2');
      if (change === 'role') useUserStore.getState().actions.setUserRole('operator');
      if (change === 'disconnect') x.transport.markDisconnected();
    });
    if (change === 'account') mounted.rerender(x.view('account-2'));
    await act(async () => resolve(awaiting));
    expect(screen.queryByRole('button', { name: 'Continue recovery in browser' })).toBeNull();
    expect(x.api.openBrowser).not.toHaveBeenCalled(); expect(x.api.resume).not.toHaveBeenCalled();
  });
});
