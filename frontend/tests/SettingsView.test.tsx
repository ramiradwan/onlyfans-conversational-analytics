import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HistorySettings } from '../src/protocol';
import type { DesktopState } from '../onboarding/client.js';
import type { DesktopOnboardingApi } from '../src/services/desktopOnboarding';
import { HistorySettingsApiError, type HistorySettingsApi } from '../src/services/historySettingsApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';
import SettingsView from '../src/views/SettingsView';

const initial: HistorySettings = {
  creator_account_id: 'creator-1',
  settings_revision: 1,
  consent_policy_version: 'history-consent-v1',
  consent_revision: null,
  authorized_platform_creator_id: null,
  desired_state: 'not_started',
  effective_state: 'not_applied',
  effective_config_revision: null,
  recent_window_days: 30,
  page_size: 50,
  pages_per_wake: 2,
  request_interval_ms: 1000,
  retry_limit: 3,
  updated_at: '2026-07-19T12:00:00Z',
};

beforeEach(() => {
  useUserStore.getState().actions.setUserRole('creator-ceo');
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  useUserStore.getState().actions.setUserRole(null);
  bridgeTransportStore.reset();
});

function connectExtension() {
  bridgeTransportStore.bindAccount('creator-1');
  bridgeTransportStore.setConnection('connected');
  bridgeTransportStore.setAgent({
    creator_account_id: 'creator-1',
    status: 'connected',
    agent_installation_id: '90000000-0000-4000-8000-000000000002',
    connection_id: '90000000-0000-4000-8000-000000000003',
    required_config_revision: 'config-1',
    applied_config_revision: 'config-1',
    required_history_settings_revision: 1,
    applied_history_settings_revision: 1,
    last_heartbeat_at: '2026-07-19T12:00:00Z',
    degraded_reason: null,
    browser: null,
  });
}

describe('SettingsView history consent', () => {
  it('clears the pending change once the extension applies it and stops refreshing', async () => {
    const running: HistorySettings = {
      ...initial, settings_revision: 2, consent_revision: 'consent-1',
      desired_state: 'running', effective_state: 'running',
    };
    const pending: HistorySettings = {
      ...running, settings_revision: 3, desired_state: 'paused',
    };
    const applied: HistorySettings = { ...pending, effective_state: 'paused' };
    const api: HistorySettingsApi = {
      get: vi.fn().mockResolvedValueOnce(running).mockResolvedValue(applied),
      update: vi.fn().mockResolvedValue(pending),
      revoke: vi.fn(),
    };
    connectExtension();
    render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
    await screen.findByRole('button', { name: 'Pause' });
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Pause' })); });
    expect(screen.getByText('Waiting for the extension to confirm the pause.')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.queryByText('Waiting for the extension to confirm the pause.')).toBeNull();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(api.get).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('does not let an earlier acknowledgement read overwrite a newer user change', async () => {
    const running: HistorySettings = {
      ...initial, settings_revision: 2, consent_revision: 'consent-1',
      desired_state: 'running', effective_state: 'running',
    };
    const pending: HistorySettings = {
      ...running, settings_revision: 3, desired_state: 'paused',
    };
    let resolveOldRead!: (value: HistorySettings) => void;
    const oldRead = new Promise<HistorySettings>((resolve) => { resolveOldRead = resolve; });
    const api: HistorySettingsApi = {
      get: vi.fn().mockResolvedValueOnce(running).mockReturnValueOnce(oldRead),
      update: vi.fn().mockResolvedValueOnce(pending)
        .mockResolvedValueOnce({ ...running, settings_revision: 4 }),
      revoke: vi.fn(),
    };
    connectExtension();
    render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
    await screen.findByRole('button', { name: 'Pause' });
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Pause' })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(api.get).toHaveBeenCalledTimes(2);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Resume' })); });
    await act(async () => { resolveOldRead({ ...pending, effective_state: 'paused' }); });
    expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
    expect(api.update).toHaveBeenLastCalledWith(3, expect.objectContaining({ desired_state: 'running' }), expect.any(AbortSignal));
  });

  it('asks for consent only once the browser extension is connected', async () => {
    const api: HistorySettingsApi = {
      get: vi.fn(async () => initial),
      update: vi.fn(async () => initial),
      revoke: vi.fn(async () => initial),
    };

    const view = render(
      <ThemeProvider theme={theme} defaultMode="light">
        <SettingsView api={api} />
      </ThemeProvider>,
    );
    expect(await screen.findByText('Available once the browser extension is connected.')).toBeTruthy();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Turn on message history' })).toBeNull();

    view.unmount();
    connectExtension();
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <SettingsView api={api} />
      </ThemeProvider>,
    );
    expect(await screen.findByRole('button', { name: 'Turn on message history' })).toBeTruthy();
    expect(screen.queryByText('Available once the browser extension is connected.')).toBeNull();
  });

  it('requires explicit consent, then exposes pause/resume and revocation through the REST service', async () => {
    const running: HistorySettings = {
      ...initial,
      settings_revision: 2,
      consent_revision: 'consent-1',
      authorized_platform_creator_id: 'platform-creator-1',
      desired_state: 'running',
      effective_state: 'running',
      effective_config_revision: 'config-2',
    };
    const revoked: HistorySettings = {
      ...running,
      settings_revision: 3,
      consent_revision: null,
      authorized_platform_creator_id: null,
      desired_state: 'revoked',
      effective_state: 'revoked',
    };
    const api: HistorySettingsApi = {
      get: vi.fn(async () => initial),
      update: vi.fn(async () => running),
      revoke: vi.fn(async () => revoked),
    };

    connectExtension();
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <SettingsView api={api} />
      </ThemeProvider>,
    );

    const start = await screen.findByRole('button', { name: 'Turn on message history' });
    expect(start.hasAttribute('disabled')).toBe(true);
    fireEvent.click(
      screen.getByRole('checkbox', {
        name: /I allow read-only syncing of my older messages/,
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' }));

    await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1));
    expect(api.update).toHaveBeenCalledWith(
      1,
      expect.objectContaining({
        desired_state: 'running',
        accept_consent: true,
        consent_policy_version: 'history-consent-v1',
      }),
      expect.any(AbortSignal),
    );
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeTruthy();
    expect(screen.queryByText('platform-creator-1')).toBeNull();
    expect(screen.queryByText('history-consent-v1')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }));
    expect(api.revoke).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Turn off' }));
    await waitFor(() => expect(api.revoke).toHaveBeenCalledWith(2, expect.any(AbortSignal)));
  });

  it('shows status but withholds mutation controls from operators', async () => {
    useUserStore.getState().actions.setUserRole('operator');
    const api: HistorySettingsApi = {
      get: vi.fn(async () => initial),
      update: vi.fn(),
      revoke: vi.fn(),
    };
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <SettingsView api={api} />
      </ThemeProvider>,
    );
    expect(await screen.findByText('Only the account owner can change message history.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Turn on message history' })).toBeNull();
  });

  it('requests browser permission inside the consent flow and waits for committed permission', async () => {
    connectExtension();
    const agent = bridgeTransportStore.getState().agent!;
    bridgeTransportStore.setAgent({ ...agent, browser: { capture: 'active', site_access: 'granted',
      history_permission: 'missing', legal_review_required: false, reported_at: initial.updated_at } });
    const running: HistorySettings = { ...initial, consent_revision: 'consent-1', settings_revision: 2,
      authorized_platform_creator_id: 'platform-creator-1', desired_state: 'running', effective_state: 'running' };
    const api = { get: vi.fn(async () => initial), update: vi.fn(async () => running), revoke: vi.fn() };
    const browserApi = { open: vi.fn(async () => 'delivered' as const), setCapture: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={api} browserApi={browserApi} /></ThemeProvider>);
    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' }));
    await waitFor(() => expect(browserApi.open).toHaveBeenCalledExactlyOnceWith('history', expect.any(AbortSignal)));
    expect(screen.getByText('Permission needed')).toBeTruthy();
    expect(screen.queryByText('On', { exact: true })).toBeNull();
    await act(async () => bridgeTransportStore.setAgent({ ...agent, browser: { capture: 'active', site_access: 'granted',
      history_permission: 'granted', legal_review_required: false, reported_at: initial.updated_at } }));
    expect(screen.queryByRole('button', { name: 'Allow Chrome access' })).toBeNull();
    expect(screen.getByText('Waiting', { exact: true })).toBeTruthy();
    expect(api.update).toHaveBeenCalledOnce();
  });

  it('bounds acknowledgement reads while retaining the unconfirmed state', async () => {
    vi.useFakeTimers();
    connectExtension();
    const pending: HistorySettings = { ...initial, consent_revision: 'consent-1', desired_state: 'running' };
    const api = { get: vi.fn(async () => pending), update: vi.fn(), revoke: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
    await act(async () => {});
    expect(screen.getByText('Starting', { exact: true })).toBeTruthy();
    for (let attempt = 0; attempt < 4; attempt++) await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(api.get).toHaveBeenCalledTimes(4);
    expect(screen.queryByText('On', { exact: true })).toBeNull();
  });

  it('reads a conflicting change automatically without replaying consent', async () => {
    connectExtension();
    const changed: HistorySettings = { ...initial, settings_revision: 3, consent_revision: 'consent-1',
      desired_state: 'paused', effective_state: 'paused' };
    const api = { get: vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(changed),
      update: vi.fn().mockRejectedValue(new HistorySettingsApiError('Conflict', 409)), revoke: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' }));
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(api.update).toHaveBeenCalledOnce();
    expect(screen.queryByText(/Reload the page/)).toBeNull();
  });
});

describe('native history permission return', () => {
  async function prepare(delivery: 'delivered' | 'unreachable' = 'delivered') {
    connectExtension();
    const invoke = vi.fn(async () => undefined);
    vi.stubGlobal('__TAURI__', { core: { invoke } });
    const listeners = new Set<() => void>();
    let desktopState: DesktopState = { connected: true, pending: null, error: null, snapshot: {
      journey_id: 'journey-1', generation: 'generation-1', revision: 1, facts: {},
      local_ready: true, full_ready: true, next_task: 'complete', allowed_actions: [], pending_operations: [],
    } };
    const desktopApi: DesktopOnboardingApi = { getState: () => desktopState,
      subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
      begin: vi.fn(), reconcile: vi.fn(), openBrowser: vi.fn() };
    const publish = (change: Partial<NonNullable<DesktopState['snapshot']>>) => {
      desktopState = { ...desktopState, snapshot: { ...desktopState.snapshot!, ...change } };
      listeners.forEach(listener => listener());
    };
    const permission = (state: 'missing' | 'granted', creator = 'creator-1') => {
      const agent = bridgeTransportStore.getState().agent!;
      bridgeTransportStore.setAgent({ ...agent, creator_account_id: creator, browser: { capture: 'active',
        site_access: 'granted', history_permission: state, legal_review_required: false, reported_at: initial.updated_at } });
    };
    permission('missing');
    const settings: HistorySettings = { ...initial, consent_revision: 'consent-1', desired_state: 'running', effective_state: 'running' };
    const api = { get: vi.fn(async () => settings), update: vi.fn(), revoke: vi.fn() };
    const browserApi = { open: vi.fn(async () => delivery), setCapture: vi.fn() };
    const view = render(<ThemeProvider theme={theme}><SettingsView api={api} browserApi={browserApi} desktopApi={desktopApi} /></ThemeProvider>);
    await screen.findByRole('button', { name: 'Allow Chrome access' });
    return { invoke, publish, permission, view, browserApi };
  }

  it('focuses once after a requested, confirmed grant, never from mounting or duplicate pushes', async () => {
    const { invoke, permission } = await prepare();
    expect(invoke).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Allow Chrome access' })));
    expect(invoke).not.toHaveBeenCalled();
    await act(async () => permission('granted'));
    await act(async () => permission('granted'));
    expect(invoke).toHaveBeenCalledExactlyOnceWith('desktop_window', { action: 'focus' });
  });

  it('single-flights duplicate clicks while the browser request is unresolved', async () => {
    const { invoke, permission, browserApi } = await prepare();
    let resolve!: (value: 'delivered') => void;
    browserApi.open.mockImplementation(() => new Promise(done => { resolve = done; }));
    const button = screen.getByRole('button', { name: 'Allow Chrome access' });
    await act(async () => { fireEvent.click(button); fireEvent.click(button); });
    expect(button.hasAttribute('disabled')).toBe(true);
    expect(browserApi.open).toHaveBeenCalledOnce();
    await act(async () => permission('granted'));
    expect(invoke).not.toHaveBeenCalled();
    await act(async () => resolve('delivered'));
    expect(invoke).toHaveBeenCalledExactlyOnceWith('desktop_window', { action: 'focus' });
  });

  it.each(['journey', 'generation', 'creator', 'expired', 'failed', 'unmounted', 'unsolicited'] as const)(
    'does not focus after %s permission results', async reason => {
      const { invoke, permission, publish, view } = await prepare(reason === 'failed' ? 'unreachable' : 'delivered');
      if (reason !== 'unsolicited') await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Allow Chrome access' })));
      if (reason === 'journey') await act(async () => publish({ journey_id: 'journey-2' }));
      if (reason === 'generation') await act(async () => publish({ generation: 'generation-2' }));
      if (reason === 'creator') await act(async () => bridgeTransportStore.bindAccount('creator-2'));
      if (reason === 'expired') {
        vi.useFakeTimers();
        vi.spyOn(performance, 'now').mockReturnValue(Number.MAX_SAFE_INTEGER);
      }
      if (reason === 'unmounted') view.unmount();
      await act(async () => permission('granted', reason === 'creator' ? 'creator-2' : 'creator-1'));
      expect(invoke).not.toHaveBeenCalled();
      vi.restoreAllMocks();
    },
  );
});

describe('history change reconciliation', () => {
  const authorized: HistorySettings = { ...initial, settings_revision: 2,
    consent_revision: 'consent-1', authorized_platform_creator_id: 'platform-creator-1',
    desired_state: 'running', effective_state: 'running' };

  it.each([409, 412, null])('accepts the already-saved intended effect without repeating the write or opening Chrome (%s)', async status => {
    connectExtension();
    const agent = bridgeTransportStore.getState().agent!;
    bridgeTransportStore.setAgent({ ...agent, browser: { capture: 'active', site_access: 'granted',
      history_permission: 'missing', legal_review_required: false, reported_at: initial.updated_at } });
    const api = { get: vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(authorized),
      update: vi.fn().mockRejectedValue(new HistorySettingsApiError('Lost outcome', status)), revoke: vi.fn() };
    const browserApi = { open: vi.fn(), setCapture: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={api} browserApi={browserApi} /></ThemeProvider>);
    fireEvent.click(await screen.findByRole('checkbox'));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' })));
    expect(await screen.findByText('Permission needed')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Allow Chrome access' })).toHaveProperty('disabled', false);
    expect(screen.queryByText("Your change hasn't been confirmed.")).toBeNull();
    expect(screen.queryByText('Message history settings changed.')).toBeNull();
    expect(api.update).toHaveBeenCalledOnce();
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(browserApi.open).not.toHaveBeenCalled();
  });

  it('requires new consent for the current policy and preserves the current configuration', async () => {
    connectExtension();
    const current = { ...initial, settings_revision: 3, consent_policy_version: 'history-consent-v2', page_size: 20 };
    const api = { get: vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(current),
      update: vi.fn().mockRejectedValueOnce(new HistorySettingsApiError('Changed policy', 422))
        .mockResolvedValue({ ...authorized, settings_revision: 4, consent_policy_version: 'history-consent-v2', page_size: 20 }),
      revoke: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
    fireEvent.click(await screen.findByRole('checkbox'));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' })));
    expect(screen.getByRole('checkbox')).toHaveProperty('checked', false);
    expect(screen.getByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
    expect(api.update).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('checkbox'));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' })));
    expect(api.update).toHaveBeenNthCalledWith(2, 3, expect.objectContaining({
      consent_policy_version: 'history-consent-v2', page_size: 20, accept_consent: true
    }), expect.any(AbortSignal));
  });
});

it('keeps an unconfirmed write disabled until a later authoritative revision is available', async () => {
  connectExtension();
  const saved: HistorySettings = { ...initial, settings_revision: 2, consent_revision: 'consent-1',
    authorized_platform_creator_id: 'platform-creator-1', desired_state: 'running', effective_state: 'running' };
  const api = { get: vi.fn().mockResolvedValue(initial), update: vi.fn().mockRejectedValue(new TypeError('lost response')), revoke: vi.fn() };
  render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
  fireEvent.click(await screen.findByRole('checkbox'));
  vi.useFakeTimers();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' })));
  expect(screen.getByText("Your change hasn't been confirmed.")).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
  for (let i = 0; i < 4; i++) await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(api.get).toHaveBeenCalledTimes(5);
  expect(api.update).toHaveBeenCalledOnce();
  api.get.mockResolvedValue(saved);
  await act(async () => { window.dispatchEvent(new Event('focus')); await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy();
  expect(screen.queryByText("Your change hasn't been confirmed.")).toBeNull();
  expect(api.update).toHaveBeenCalledOnce();
});

it('reconciles an already revoked receipt without issuing a second revocation', async () => {
  connectExtension();
  const running: HistorySettings = { ...initial, settings_revision: 2, consent_revision: 'consent-1',
    authorized_platform_creator_id: 'platform-creator-1', desired_state: 'running', effective_state: 'running' };
  const revoked: HistorySettings = { ...initial, settings_revision: 3, desired_state: 'revoked', effective_state: 'revoked' };
  const api = { get: vi.fn().mockResolvedValueOnce(running).mockResolvedValue(revoked),
    update: vi.fn(), revoke: vi.fn().mockRejectedValue(new HistorySettingsApiError('Lost response', 409)) };
  render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
  fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }));
  const confirm = within(screen.getByRole('dialog')).getByRole('button', { name: 'Turn off' });
  await act(async () => { fireEvent.click(confirm); fireEvent.click(confirm); });
  expect(api.revoke).toHaveBeenCalledExactlyOnceWith(2, expect.any(AbortSignal));
  expect(await screen.findByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
  expect(screen.queryByText("Your change hasn't been confirmed.")).toBeNull();
  expect(api.update).not.toHaveBeenCalled();
});

it('discards an interrupted change when the creator account changes', async () => {
  connectExtension();
  let complete!: (value: HistorySettings) => void;
  const second = { ...initial, creator_account_id: 'creator-2' };
  const api = { get: vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(second),
    update: vi.fn(() => new Promise<HistorySettings>(resolve => { complete = resolve; })), revoke: vi.fn() };
  render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
  fireEvent.click(await screen.findByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' }));
  await act(async () => bridgeTransportStore.bindAccount('creator-2'));
  await act(async () => complete({ ...initial, settings_revision: 2, consent_revision: 'receipt',
    authorized_platform_creator_id: 'platform-creator-1', desired_state: 'running', effective_state: 'running' }));
  expect((api.update.mock.calls[0] as unknown as [number, unknown, AbortSignal])[2].aborted).toBe(true);
  expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull();
  expect(screen.queryByText("Your change hasn't been confirmed.")).toBeNull();
  expect(api.update).toHaveBeenCalledOnce();
});

it.each([401, 403, 422])('does not repeat a definite history settings read refusal (%s)', async status => {
  connectExtension();
  vi.useFakeTimers();
  const api = { get: vi.fn().mockRejectedValue(new HistorySettingsApiError('Refused', status)),
    update: vi.fn(), revoke: vi.fn() };
  render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  expect(api.get).toHaveBeenCalledOnce();
  expect(screen.getByText("Message history settings couldn't be loaded.")).toBeTruthy();
  expect(api.update).not.toHaveBeenCalled();
});

it('does not lock a definitely refused history change as an unknown write', async () => {
  connectExtension();
  const api = { get: vi.fn().mockResolvedValue(initial),
    update: vi.fn().mockRejectedValue(new HistorySettingsApiError('Refused', 403)), revoke: vi.fn() };
  render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
  fireEvent.click(await screen.findByRole('checkbox'));
  vi.useFakeTimers();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Turn on message history' })));
  expect(screen.getByText("Dipsy couldn't change message history settings.")).toBeTruthy();
  expect(screen.queryByText("Your change hasn't been confirmed.")).toBeNull();
  expect(screen.getByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(api.get).toHaveBeenCalledTimes(2);
  expect(api.update).toHaveBeenCalledOnce();
});
