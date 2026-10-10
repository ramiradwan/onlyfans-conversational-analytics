import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CompanionPairingControls } from '../src/components/CompanionPairingControls';
import type { CompanionPairingApi, CompanionPairingStatus } from '../src/services/companionPairingApi';
import type { ExtensionPort, ExtensionPortState } from '../src/services/extensionPort';
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
    pins: vi.fn(async () => []),
    revoke: vi.fn(async () => ({ ...awaiting, state: 'revoked' })),
    open: vi.fn(async () => open),
    get: vi.fn(async () => awaiting),
    change: vi.fn(async (_id, action) => ({
      ...awaiting, state: action === 'confirm' ? 'admitted' : action === 'decline' ? 'declined' : 'cancelled',
      version: 4,
    })),
    confirmVerified: vi.fn(async () => ({ ...awaiting, state: 'admitted' as const, version: 4 })),
    ...overrides,
  };
}

// A port whose state the test pushes, as the extension would.
function makePort(initial: ExtensionPortState = { status: 'absent', stage: null, attempt: null }) {
  let state = initial;
  const listeners = new Set<() => void>();
  const port = {
    getState: () => state,
    subscribe: vi.fn((listener: () => void) => { listeners.add(listener); return () => listeners.delete(listener); }),
    open: vi.fn(() => true),
    pair: vi.fn(() => true),
    cancel: vi.fn(() => true),
    retry: vi.fn(),
  } satisfies ExtensionPort;
  const push = (next: Partial<ExtensionPortState>) => {
    state = { ...state, ...next };
    listeners.forEach((listener) => listener());
  };
  return { port, push };
}

let revision = 0;
// Brain's change notice over the Bridge WebSocket.
async function brainNotice() {
  revision += 1;
  await act(async () => bridgeTransportStore.setCompanion({
    creator_account_id: 'creator-1', revision, changed_at: now.toISOString(),
  }));
}

function mount(api: CompanionPairingApi, port: ExtensionPort = makePort().port) {
  return render(<ThemeProvider theme={theme}><CompanionPairingControls api={api} port={port} /></ThemeProvider>);
}

async function click(name: string) {
  await act(async () => fireEvent.click(screen.getByRole('button', { name })));
}

beforeEach(() => {
  bridgeTransportStore.reset();
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
  it('removes the connected badge when the bridge is lost and restores it on recovery', async () => {
    bridgeTransportStore.setConnection('connected');
    bridgeTransportStore.setAgent({ creator_account_id: 'creator-1', status: 'connected', agent_installation_id: null, connection_id: null,
      required_config_revision: 'r1', applied_config_revision: 'r1', required_history_settings_revision: 1,
      applied_history_settings_revision: 1, last_heartbeat_at: null, degraded_reason: null, browser: null });
    mount(makeApi({ pins: vi.fn(async () => [{ ...awaiting, state: 'admitted', version: 4, comparison_code: null }]) }));
    await act(async () => {});
    expect(screen.getByText('Connected', { exact: true })).toBeTruthy();
    await act(async () => bridgeTransportStore.setConnection('disconnected'));
    expect(screen.queryByText('Connected', { exact: true })).toBeNull();
    await act(async () => bridgeTransportStore.setConnection('connected'));
    expect(screen.getByText('Connected', { exact: true })).toBeTruthy();
  });
  it('restores connected extensions and disconnects only the displayed account connection', async () => {
    const pin = { ...awaiting, state: 'admitted' as const, version: 4, comparison_code: null };
    const api = makeApi({ pins: vi.fn(async () => [pin]) });
    mount(api);
    await act(async () => {});
    await click('Disconnect browser extension 1');
    expect(api.revoke).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Disconnect' })));
    expect(api.revoke).toHaveBeenCalledWith(pin.pairing_id, 4, expect.any(AbortSignal));
    expect(screen.queryByRole('button', { name: 'Disconnect browser extension 1' })).toBeNull();
  });

  it('shows the linked browser state pushed through Brain, with controls only for the creator', async () => {
    const pin = { ...awaiting, state: 'admitted' as const, version: 4, comparison_code: null };
    const api = makeApi({ pins: vi.fn(async () => [pin]) });
    bridgeTransportStore.setConnection('connected');
    await act(async () => bridgeTransportStore.setAgent({
      creator_account_id: 'creator-1', status: 'connected', agent_installation_id: null, connection_id: null,
      required_config_revision: 'r1', applied_config_revision: 'r1', required_history_settings_revision: 1,
      applied_history_settings_revision: 1, last_heartbeat_at: null, degraded_reason: null,
      browser: { capture: 'paused', site_access: 'granted', history_permission: 'granted', legal_review_required: false, reported_at: now.toISOString() },
    }));
    mount(api);
    await act(async () => {});
    expect(screen.getByText('Paused. Nothing new is collected.')).toBeTruthy();
    expect(screen.getByText('Paused', { exact: true })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Resume collecting' })).toBeNull();
    await act(async () => useUserStore.getState().actions.setUserRole('creator-ceo'));
    expect(screen.getByRole('button', { name: 'Resume collecting' })).toBeTruthy();
    expect(screen.queryByText(/managed in the extension/)).toBeNull();
  });

  it('refuses a connection list from another account', async () => {
    const api = makeApi({ pins: vi.fn(async () => [{ ...awaiting, state: 'admitted', creator_account_id: 'other' }]) });
    mount(api);
    await act(async () => {});
    expect(screen.getByText("Connected extensions couldn't be checked.")).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Disconnect browser extension 1' })).toBeNull();
  });

  it('requires explicit code comparison before versioned confirmation without exposing the identity thumbprint', async () => {
    const api = makeApi();
    mount(api);
    await click('Connect extension');
    expect(api.open).toHaveBeenCalledWith('creator-1', expect.any(AbortSignal));
    expect(screen.queryByLabelText('Connection comparison code')).toBeNull();
    expect(screen.getByText(/Connection window expires in 5:00/)).toBeTruthy();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(api.get).not.toHaveBeenCalled();
    await brainNotice();
    expect(screen.getByLabelText('Connection comparison code').textContent).toBe('012 345');
    expect(screen.getByText(/Code expires in 4:59/)).toBeTruthy();
    expect(screen.queryByText(awaiting.agent_identity_thumbprint!)).toBeNull();
    expect(screen.queryByText(/Extension identity:/)).toBeNull();
    expect((screen.getByRole('button', { name: 'Confirm connection' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('checkbox'));
    await click('Confirm connection');
    expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'confirm', 3, expect.any(AbortSignal));
    expect(screen.getByText(/Connection approved. Waiting for the extension./)).toBeTruthy();
    expect(screen.getByText(/Waiting for the extension/)).toBeTruthy();
    await act(async () => vi.advanceTimersByTimeAsync(300_000));
    await brainNotice();
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText('Connection comparison code')).toBeNull();
  });

  it('declines mismatched codes without requiring acceptance', async () => {
    const api = makeApi({ open: vi.fn(async () => awaiting) });
    mount(api);
    await click('Connect extension');
    await click("Codes don't match");
    expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'decline', 3, expect.any(AbortSignal));
    expect(screen.getByText(/codes didn't match, so nothing was connected/)).toBeTruthy();
  });

  it('preserves comparison acceptance across unchanged notices and resets it when the code changes', async () => {
    const get = vi.fn()
      .mockResolvedValueOnce({ ...awaiting })
      .mockResolvedValueOnce({ ...awaiting, comparison_code: '987654' });
    const api = makeApi({ open: vi.fn(async () => awaiting), get });
    mount(api);
    await click('Connect extension');
    fireEvent.click(screen.getByRole('checkbox'));
    await brainNotice();
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('button', { name: 'Confirm connection' }) as HTMLButtonElement).disabled).toBe(false);
    await brainNotice();
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('button', { name: 'Confirm connection' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('cancels a pending window and ignores later notices', async () => {
    const api = makeApi();
    mount(api);
    await click('Connect extension');
    await click('Cancel');
    expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'cancel', 0, expect.any(AbortSignal));
    await brainNotice();
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
    await click('Connect extension');
    await brainNotice();
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText('Time ran out before the connection finished. Try again.')).toBeTruthy();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(awaiting));
    expect(screen.queryByLabelText('Connection comparison code')).toBeNull();
  });

  it('aborts a pending read and cancels the known window when the account changes without exposing raw account IDs', async () => {
    let resolve!: (value: CompanionPairingStatus) => void;
    let signal: AbortSignal | undefined;
    const api = makeApi({ get: vi.fn((_id, nextSignal) => {
      signal = nextSignal;
      return new Promise((done) => { resolve = done; });
    }) });
    mount(api);
    await click('Connect extension');
    await brainNotice();
    await act(async () => bridgeTransportStore.bindAccount('creator-2'));
    expect(signal?.aborted).toBe(true);
    expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'cancel', 0);
    await act(async () => resolve(awaiting));
    expect(screen.queryByText(/creator-2/)).toBeNull();
    expect(screen.queryByLabelText('Connection comparison code')).toBeNull();
  });

  it('aborts opening on unmount without accepting its late completion', async () => {
    let resolve!: (value: CompanionPairingStatus) => void;
    let signal: AbortSignal | undefined;
    const api = makeApi({ open: vi.fn((_id, nextSignal) => {
      signal = nextSignal;
      return new Promise((done) => { resolve = done; });
    }) });
    const view = mount(api);
    await click('Connect extension');
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
    await click('Connect extension');
    fireEvent.click(screen.getByRole('checkbox'));
    await brainNotice();
    expect(screen.getByRole('alert').textContent).toContain("The connection couldn't be checked");
    expect(screen.queryByText('untrusted error body')).toBeNull();
    expect(screen.queryByLabelText('Connection comparison code')).toBeNull();
    await brainNotice();
    expect(get).toHaveBeenCalledTimes(1);
    await click('Try again');
    expect(screen.getByLabelText('Connection comparison code')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Confirm connection' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('rejects status reads for another account before showing their code', async () => {
    const api = makeApi({ get: vi.fn(async () => ({ ...awaiting, creator_account_id: 'other-account' })) });
    mount(api);
    await click('Connect extension');
    await brainNotice();
    expect(screen.queryByLabelText('Connection comparison code')).toBeNull();
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  it('requires an authenticated presentation role before opening', () => {
    useUserStore.getState().actions.setUserRole(null);
    const api = makeApi();
    mount(api);
    expect(screen.queryByRole('button', { name: 'Connect extension' })).toBeNull();
    expect(api.open).not.toHaveBeenCalled();
  });

  describe('when the extension answers in this browser', () => {
    const ready: ExtensionPortState = { status: 'connected', stage: 'ready_to_pair', attempt: null };

    it('pairs with one click: Brain opens first, then the extension, and Brain checks the reported code', async () => {
      const { port, push } = makePort(ready);
      const api = makeApi();
      mount(api, port);
      await click('Connect extension');
      expect(api.open).toHaveBeenCalledWith('creator-1', expect.any(AbortSignal));
      expect(port.pair).toHaveBeenCalledTimes(1);
      expect(vi.mocked(api.open).mock.invocationCallOrder[0])
        .toBeLessThan(port.pair.mock.invocationCallOrder[0]);
      expect(screen.getByText('Connecting the extension in this browser…')).toBeTruthy();
      expect(screen.getByText('Connecting', { exact: true })).toBeTruthy();
      expect(screen.queryByText('Not connected')).toBeNull();
      await act(async () => push({ stage: 'pairing', attempt: { state: 'compare', comparison_code: '012345' } }));
      expect(api.confirmVerified).not.toHaveBeenCalled();
      await brainNotice();
      expect(api.confirmVerified).toHaveBeenCalledWith(open.pairing_id, 3, '012345', expect.any(AbortSignal));
      expect(api.confirmVerified).toHaveBeenCalledTimes(1);
      expect(screen.getByText(/Connection approved. Waiting for the extension./)).toBeTruthy();
      expect(screen.queryByRole('checkbox')).toBeNull();
      expect(screen.queryByLabelText('Connection comparison code')).toBeNull();
    });

    it('preserves confirmation when the setup view unmounts before its reply', async () => {
      let finish!: (value: CompanionPairingStatus) => void;
      let signal: AbortSignal | undefined;
      const api = makeApi({ confirmVerified: vi.fn((_id, _version, _code, controller) => {
        signal = controller;
        return new Promise<CompanionPairingStatus>((resolve) => { finish = resolve; });
      }) });
      const { port, push } = makePort(ready);
      const view = mount(api, port);
      await click('Connect extension');
      await act(async () => push({ stage: 'pairing', attempt: { state: 'compare', comparison_code: '012345' } }));
      await brainNotice();
      expect(api.confirmVerified).toHaveBeenCalledTimes(1);
      await act(async () => view.unmount());
      expect(signal?.aborted).toBe(false);
      expect(port.cancel).not.toHaveBeenCalled();
      expect(api.change).not.toHaveBeenCalled();
      await act(async () => finish({ ...awaiting, state: 'admitted', version: 4, comparison_code: null }));
      expect(api.change).not.toHaveBeenCalled();
    });

    it('keeps an admitted connection when the setup view unmounts', async () => {
      const { port, push } = makePort(ready);
      const api = makeApi();
      const view = mount(api, port);
      await click('Connect extension');
      await act(async () => push({ stage: 'pairing', attempt: { state: 'compare', comparison_code: '012345' } }));
      await brainNotice();
      await act(async () => {});
      expect(api.confirmVerified).toHaveBeenCalledTimes(1);
      view.unmount();
      expect(port.cancel).not.toHaveBeenCalled();
      expect(api.change).not.toHaveBeenCalled();
    });

    it('opens the extension setup tab for unfinished steps and continues once they are done', async () => {
      const { port, push } = makePort({ status: 'connected', stage: 'needs_full', attempt: null });
      const api = makeApi();
      mount(api, port);
      await click('Connect extension');
      expect(port.open).toHaveBeenCalledWith('setup');
      expect(api.open).not.toHaveBeenCalled();
      expect(screen.getByText(/Turn on Full analytics in the extension setup tab/)).toBeTruthy();
      await act(async () => push({ stage: 'needs_site_access' }));
      expect(screen.getByText(/Allow site access in the extension setup tab/)).toBeTruthy();
      await act(async () => push({ stage: 'ready_to_pair' }));
      expect(api.open).toHaveBeenCalledTimes(1);
      expect(port.pair).toHaveBeenCalledTimes(1);
    });

    it('ends Brain\'s window when the extension stops its attempt', async () => {
      const { port, push } = makePort(ready);
      const api = makeApi();
      mount(api, port);
      await click('Connect extension');
      await act(async () => push({ attempt: { state: 'failed', comparison_code: null } }));
      expect(api.change).toHaveBeenCalledWith(open.pairing_id, 'cancel', 0, expect.any(AbortSignal));
      expect(screen.getByText('The extension stopped the connection. Try again.')).toBeTruthy();
    });

    it('cancels both sides and closes nothing it did not open', async () => {
      const { port } = makePort({ status: 'connected', stage: 'needs_terms', attempt: null });
      const api = makeApi();
      mount(api, port);
      await click('Connect extension');
      await click('Cancel');
      expect(port.cancel).toHaveBeenCalled();
      expect(api.change).not.toHaveBeenCalled();
      expect(screen.getByRole('button', { name: 'Connect extension' })).toBeTruthy();
    });
  });

  it('falls back to comparing codes by eye when no extension answers in this browser', async () => {
    const { port } = makePort();
    const api = makeApi();
    mount(api, port);
    await click('Connect extension');
    expect(port.pair).not.toHaveBeenCalled();
    expect(screen.getByText(/open its setup tab and choose Connect extension/)).toBeTruthy();
  });
});
