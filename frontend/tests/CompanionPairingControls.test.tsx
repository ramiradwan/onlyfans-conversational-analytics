import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CompanionPairingControls } from '../src/components/CompanionPairingControls';
import type { CompanionPairingApi, CompanionPairingStatus } from '../src/services/companionPairingApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const now = new Date('2026-09-12T12:00:00Z');
const open: CompanionPairingStatus = {
  pairing_id: 'A'.repeat(43), creator_account_id: 'creator-1', generation: 1, version: 0,
  state: 'open', expires_at: '2026-09-12T12:05:00Z', comparison_code: null,
  agent_identity_thumbprint: null,
};
const awaiting: CompanionPairingStatus = {
  ...open, state: 'awaiting_confirmation', version: 3,
  comparison_code: '012345', agent_identity_thumbprint: 'B'.repeat(43),
};

function makeApi(overrides: Partial<CompanionPairingApi> = {}): CompanionPairingApi {
  return {
    open: vi.fn(async () => open),
    get: vi.fn(async () => awaiting),
    change: vi.fn(async (_id, action) => ({
      ...awaiting, state: action === 'confirm' ? 'admitted' : action === 'decline' ? 'declined' : 'cancelled',
      version: 4,
    })),
    ...overrides,
  };
}

function mount(api: CompanionPairingApi) {
  return render(<ThemeProvider theme={theme}><CompanionPairingControls api={api} /></ThemeProvider>);
}

async function click(name: string) {
  await act(async () => fireEvent.click(screen.getByRole('button', { name })));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
  useUserStore.getState().actions.setUserRole('operator');
  bridgeTransportStore.bindAccount('creator-1');
});
afterEach(() => {
  cleanup();
  useUserStore.getState().actions.setUserRole(null);
  vi.useRealTimers();
});

describe('companion pairing controls', () => {
  it('requires explicit code/account comparison before versioned confirmation', async () => {
    const api = makeApi();
    mount(api);
    await click('Open pairing window');
    expect(api.open).toHaveBeenCalledWith('creator-1', expect.any(AbortSignal));
    expect(screen.queryByLabelText('Pairing comparison code')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(screen.getByLabelText('Pairing comparison code').textContent).toBe('012 345');
    expect(screen.getByText(`Extension identity: ${awaiting.agent_identity_thumbprint}`)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Confirm pairing' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('checkbox'));
    await click('Confirm pairing');
    expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'confirm', 3, expect.any(AbortSignal));
    expect(screen.getByText(/Local approval is complete/)).toBeTruthy();
    expect(screen.getByText(/only after the extension establishes its encrypted companion session/)).toBeTruthy();
    await act(async () => vi.advanceTimersByTimeAsync(300_000));
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText('Pairing comparison code')).toBeNull();
  });

  it('declines mismatched codes without requiring acceptance', async () => {
    const api = makeApi({ open: vi.fn(async () => awaiting) });
    mount(api);
    await click('Open pairing window');
    await click('Codes do not match');
    expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'decline', 3, expect.any(AbortSignal));
    expect(screen.getByText('Pairing was declined.')).toBeTruthy();
  });

  it('preserves comparison acceptance across unchanged polls and resets it when the code changes', async () => {
    const get = vi.fn()
      .mockResolvedValueOnce({ ...awaiting })
      .mockResolvedValueOnce({ ...awaiting, comparison_code: '987654' });
    const api = makeApi({ open: vi.fn(async () => awaiting), get });
    mount(api);
    await click('Open pairing window');
    fireEvent.click(screen.getByRole('checkbox'));
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('button', { name: 'Confirm pairing' }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('button', { name: 'Confirm pairing' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('cancels a pending window and stops polling', async () => {
    const api = makeApi();
    mount(api);
    await click('Open pairing window');
    await click('Cancel pairing');
    expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'cancel', 0, expect.any(AbortSignal));
    await act(async () => vi.advanceTimersByTimeAsync(300_000));
    expect(api.get).not.toHaveBeenCalled();
  });

  it('stops at the window deadline even while a status request is pending', async () => {
    let resolve!: (value: CompanionPairingStatus) => void;
    let signal: AbortSignal | undefined;
    const api = makeApi({
      open: vi.fn(async () => ({ ...open, expires_at: '2026-09-12T12:00:02Z' })),
      get: vi.fn((_id, nextSignal) => {
        signal = nextSignal;
        return new Promise((done) => { resolve = done; });
      }),
    });
    mount(api);
    await click('Open pairing window');
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText('The pairing window expired.')).toBeTruthy();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(awaiting));
    expect(screen.queryByLabelText('Pairing comparison code')).toBeNull();
  });

  it('aborts a pending read and cancels the known window when the account changes', async () => {
    let resolve!: (value: CompanionPairingStatus) => void;
    let signal: AbortSignal | undefined;
    const api = makeApi({ get: vi.fn((_id, nextSignal) => {
      signal = nextSignal;
      return new Promise((done) => { resolve = done; });
    }) });
    mount(api);
    await click('Open pairing window');
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    await act(async () => bridgeTransportStore.bindAccount('creator-2'));
    expect(signal?.aborted).toBe(true);
    expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'cancel', 0);
    await act(async () => resolve(awaiting));
    expect(screen.getByText('Creator account: creator-2')).toBeTruthy();
    expect(screen.queryByLabelText('Pairing comparison code')).toBeNull();
  });

  it('aborts opening on unmount without accepting its late completion', async () => {
    let resolve!: (value: CompanionPairingStatus) => void;
    let signal: AbortSignal | undefined;
    const api = makeApi({ open: vi.fn((_id, nextSignal) => {
      signal = nextSignal;
      return new Promise((done) => { resolve = done; });
    }) });
    const view = mount(api);
    await click('Open pairing window');
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(awaiting));
    expect(api.change).not.toHaveBeenCalled();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('fails closed on status errors and hides comparison details until a checked retry', async () => {
    const get = vi.fn().mockRejectedValueOnce(new Error('untrusted error body')).mockResolvedValue(awaiting);
    const api = makeApi({ open: vi.fn(async () => awaiting), get });
    mount(api);
    await click('Open pairing window');
    fireEvent.click(screen.getByRole('checkbox'));
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(screen.getByRole('alert').textContent).toContain('Pairing status could not be verified');
    expect(screen.queryByText('untrusted error body')).toBeNull();
    expect(screen.queryByLabelText('Pairing comparison code')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(get).toHaveBeenCalledTimes(1);
    await click('Check pairing status');
    expect(screen.getByLabelText('Pairing comparison code')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Confirm pairing' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('rejects polling responses for another account before showing their code', async () => {
    const api = makeApi({ get: vi.fn(async () => ({ ...awaiting, creator_account_id: 'other-account' })) });
    mount(api);
    await click('Open pairing window');
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(screen.queryByLabelText('Pairing comparison code')).toBeNull();
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  it('requires an authenticated presentation role before opening', () => {
    useUserStore.getState().actions.setUserRole(null);
    const api = makeApi();
    mount(api);
    expect(screen.queryByRole('button', { name: 'Open pairing window' })).toBeNull();
    expect(api.open).not.toHaveBeenCalled();
  });
});
