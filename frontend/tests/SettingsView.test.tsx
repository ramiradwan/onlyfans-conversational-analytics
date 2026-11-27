import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HistorySettings } from '../src/protocol';
import type { DesktopState } from '../onboarding/client.js';
import type { DesktopOnboardingApi } from '../src/services/desktopOnboarding';
import { HistorySettingsApiError, type HistorySettingsApi } from '../src/services/historySettingsApi';
import { createHistoryRecoveryJournal } from '../src/services/historyRecovery';
import { desktopFixture } from './desktopOnboardingFixture';
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

const CONSENT_LABEL = 'I allow read-only syncing of my older messages to this computer.';
const PERMISSION_PURPOSE = 'Older messages start syncing after you allow access in your browser.';

// An open dialog hides the page behind it from assistive technology, so checks made then look past that.
const historySwitch = (behindDialog = false) =>
  screen.getByRole<HTMLInputElement>('switch', { name: 'Add older messages', hidden: behindDialog });
const consentDialog = () => within(screen.getByRole('dialog', { name: 'Turn on message history?' }));

/** Settings asks for consent in a dialog opened from the history switch. */
async function openConsent() {
  const toggle = await screen.findByRole<HTMLInputElement>('switch', { name: 'Add older messages' });
  expect(toggle.checked).toBe(false);
  fireEvent.click(toggle);
  return consentDialog();
}

async function chooseTurnOff() {
  fireEvent.click(await screen.findByRole('button', { name: 'Message history options' }));
  fireEvent.click(screen.getByRole('menuitem', { name: 'Turn off message history' }));
}

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
    expect((await screen.findByRole<HTMLInputElement>('switch', { name: 'Add older messages' })).checked).toBe(true);
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(historySwitch()); });
    expect(screen.getByText('Waiting for the extension to confirm the pause.')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.queryByText('Waiting for the extension to confirm the pause.')).toBeNull();
    expect(historySwitch().checked).toBe(false);
    expect(historySwitch()).toHaveProperty('disabled', false);
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
    await screen.findByRole('switch', { name: 'Add older messages' });
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(historySwitch()); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(historySwitch().checked).toBe(false);
    await act(async () => { fireEvent.click(historySwitch()); });
    await act(async () => { resolveOldRead({ ...pending, effective_state: 'paused' }); });
    expect(historySwitch().checked).toBe(true);
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
    expect(historySwitch()).toHaveProperty('disabled', true);
    fireEvent.click(historySwitch());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();

    view.unmount();
    connectExtension();
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <SettingsView api={api} />
      </ThemeProvider>,
    );
    const dialog = await openConsent();
    expect(dialog.getByRole('checkbox', { name: CONSENT_LABEL })).toBeTruthy();
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

    let dialog = await openConsent();
    const start = dialog.getByRole('button', { name: 'Turn on message history' });
    expect(start.hasAttribute('disabled')).toBe(true);
    const disclosure = [
      'Read-only: it never sends messages or changes your account.',
      'Synced message history stays on this computer.',
      'Pause or turn it off at any time.',
    ];
    const list = dialog.getByText(disclosure[0], { exact: true }).parentElement!;
    expect([...list.children].map(item => item.textContent)).toEqual(disclosure);
    const consent = dialog.getByRole('checkbox', { name: CONSENT_LABEL });
    expect(list.compareDocumentPosition(consent) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(consent.compareDocumentPosition(start) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(consent);
    fireEvent.click(dialog.getByRole('button', { name: 'Not now' }));
    expect(api.update).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(historySwitch().checked).toBe(false);
    // Asking again starts unticked, so an earlier tick never carries over.
    dialog = await openConsent();
    expect(dialog.getByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
    fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
    fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' }));

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
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(historySwitch().checked).toBe(true);
    expect(screen.queryByText('platform-creator-1')).toBeNull();
    expect(screen.queryByText('history-consent-v1')).toBeNull();

    // Turning off withdraws consent, so it sits in the section menu and asks first.
    await chooseTurnOff();
    expect(api.revoke).not.toHaveBeenCalled();
    const decision = screen.getByRole('dialog', { name: 'Turn off message history?' });
    expect(within(decision).getByText('Older messages stop syncing. Messages already synced stay on this computer.')).toBeTruthy();
    fireEvent.click(within(decision).getByRole('button', { name: 'Keep message history on' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.revoke).not.toHaveBeenCalled();
    await chooseTurnOff();
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Turn off message history?' })).getByRole('button', { name: 'Turn off' }));
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
    const dialog = await openConsent();
    fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
    fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' }));
    await waitFor(() => expect(browserApi.open).toHaveBeenCalledExactlyOnceWith('history', expect.any(AbortSignal)));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // The row asks for the browser permission in place of the switch until the extension reports it.
    expect(screen.getByText(PERMISSION_PURPOSE)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Allow browser access' })).toBeTruthy();
    expect(screen.queryByRole('switch', { name: 'Add older messages', hidden: true })).toBeNull();
    await act(async () => bridgeTransportStore.setAgent({ ...agent, browser: { capture: 'active', site_access: 'granted',
      history_permission: 'granted', legal_review_required: false, reported_at: initial.updated_at } }));
    expect(screen.queryByRole('button', { name: 'Allow browser access', hidden: true })).toBeNull();
    expect(screen.getByText('Older message collection has not started.')).toBeTruthy();
    expect(historySwitch().checked).toBe(true);
    expect(api.update).toHaveBeenCalledOnce();
  });

  it('bounds acknowledgement reads while retaining the unconfirmed state', async () => {
    vi.useFakeTimers();
    connectExtension();
    const pending: HistorySettings = { ...initial, consent_revision: 'consent-1', desired_state: 'running' };
    const api = { get: vi.fn(async () => pending), update: vi.fn(), revoke: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
    await act(async () => {});
    const starting = 'Waiting for the extension to apply message history settings.';
    expect(screen.getByText(starting)).toBeTruthy();
    for (let attempt = 0; attempt < 4; attempt++) await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(api.get).toHaveBeenCalledTimes(4);
    expect(screen.getByText(starting)).toBeTruthy();
    expect(screen.queryByText(/^History saved/)).toBeNull();
  });

  it('reads a conflicting change automatically without replaying consent', async () => {
    connectExtension();
    const changed: HistorySettings = { ...initial, settings_revision: 3, consent_revision: 'consent-1',
      desired_state: 'paused', effective_state: 'paused' };
    const api = { get: vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(changed),
      update: vi.fn().mockRejectedValue(new HistorySettingsApiError('Conflict', 409)), revoke: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={api} /></ThemeProvider>);
    const dialog = await openConsent();
    fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
    fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' }));
    // The newer paused settings already hold consent, so the dialog closes on them without a second write.
    expect(await screen.findByText('Message history settings changed.')).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(historySwitch().checked).toBe(false);
    expect(historySwitch()).toHaveProperty('disabled', false);
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
    await screen.findByRole('button', { name: 'Allow browser access' });
    return { invoke, publish, permission, view, browserApi };
  }

  it('focuses once after a requested, confirmed grant, never from mounting or duplicate pushes', async () => {
    const { invoke, permission } = await prepare();
    expect(invoke).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Allow browser access' })));
    expect(invoke).not.toHaveBeenCalled();
    await act(async () => permission('granted'));
    await act(async () => permission('granted'));
    expect(invoke).toHaveBeenCalledExactlyOnceWith('desktop_window', { action: 'focus' });
  });

  it('single-flights duplicate clicks while the browser request is unresolved', async () => {
    const { invoke, permission, browserApi } = await prepare();
    let resolve!: (value: 'delivered') => void;
    browserApi.open.mockImplementation(() => new Promise(done => { resolve = done; }));
    const button = screen.getByRole('button', { name: 'Allow browser access' });
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
      if (reason !== 'unsolicited') await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Allow browser access' })));
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
    const dialog = await openConsent();
    fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
    await act(async () => fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' })));
    expect(await screen.findByText(PERMISSION_PURPOSE)).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByRole('button', { name: 'Allow browser access' })).toHaveProperty('disabled', false);
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
    const dialog = await openConsent();
    fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
    await act(async () => fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' })));
    // The dialog stays open on the newer policy, with the earlier tick cleared.
    expect(consentDialog().getByRole('checkbox', { name: CONSENT_LABEL })).toHaveProperty('checked', false);
    expect(consentDialog().getByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
    expect(api.update).toHaveBeenCalledOnce();
    fireEvent.click(consentDialog().getByRole('checkbox', { name: CONSENT_LABEL }));
    await act(async () => fireEvent.click(consentDialog().getByRole('button', { name: 'Turn on message history' })));
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
  const dialog = await openConsent();
  fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
  vi.useFakeTimers();
  await act(async () => fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' })));
  expect(consentDialog().getByText("Your change hasn't been confirmed.")).toBeTruthy();
  expect(consentDialog().getByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
  for (let i = 0; i < 4; i++) await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(api.get).toHaveBeenCalledTimes(5);
  expect(api.update).toHaveBeenCalledOnce();
  api.get.mockResolvedValue(saved);
  await act(async () => { window.dispatchEvent(new Event('focus')); await vi.advanceTimersByTimeAsync(1000); });
  // The authoritative read closes the dialog. Its exit transition runs on the same timers.
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(historySwitch().checked).toBe(true);
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
  await chooseTurnOff();
  const confirm = within(screen.getByRole('dialog', { name: 'Turn off message history?' })).getByRole('button', { name: 'Turn off' });
  await act(async () => { fireEvent.click(confirm); fireEvent.click(confirm); });
  expect(api.revoke).toHaveBeenCalledExactlyOnceWith(2, expect.any(AbortSignal));
  await waitFor(() => expect(historySwitch().checked).toBe(false));
  // Turning off never reopens the consent dialog.
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Message history options' })).toBeNull();
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
  const dialog = await openConsent();
  fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
  fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' }));
  await act(async () => bridgeTransportStore.bindAccount('creator-2'));
  await act(async () => complete({ ...initial, settings_revision: 2, consent_revision: 'receipt',
    authorized_platform_creator_id: 'platform-creator-1', desired_state: 'running', effective_state: 'running' }));
  expect((api.update.mock.calls[0] as unknown as [number, unknown, AbortSignal])[2].aborted).toBe(true);
  expect(screen.queryByRole('switch', { name: 'Add older messages', checked: true, hidden: true })).toBeNull();
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
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
  const dialog = await openConsent();
  fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
  vi.useFakeTimers();
  await act(async () => fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' })));
  expect(consentDialog().getByText("Dipsy couldn't change message history settings.")).toBeTruthy();
  expect(screen.queryByText("Your change hasn't been confirmed.")).toBeNull();
  expect(consentDialog().getByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(api.get).toHaveBeenCalledTimes(2);
  expect(api.update).toHaveBeenCalledOnce();
});

describe('history recovery after renderer restart', () => {
  const authorized: HistorySettings = { ...initial, settings_revision: 2,
    consent_revision: 'consent-1', authorized_platform_creator_id: 'platform-creator-1',
    desired_state: 'running', effective_state: 'running' };

  function durableStorage() {
    const values = new Map<string, string>();
    return {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
      removeItem: (key: string) => { values.delete(key); },
    };
  }

  function desktopOwner() {
    const source = desktopFixture();
    source.readiness({ schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active',
      analysis_admission: 'admitted' }, 'creator-1');
    return source;
  }

  it('reads current history after restart without replaying an interrupted consent request or opening Chrome', async () => {
    connectExtension();
    const agent = bridgeTransportStore.getState().agent!;
    bridgeTransportStore.setAgent({ ...agent, browser: { capture: 'active', site_access: 'granted',
      history_permission: 'missing', legal_review_required: false, reported_at: initial.updated_at } });
    const storage = durableStorage();
    const owner = desktopOwner();
    let complete!: (next: HistorySettings) => void;
    const api = { get: vi.fn(async () => initial),
      update: vi.fn(() => new Promise<HistorySettings>(resolve => { complete = resolve; })), revoke: vi.fn() };
    const browserApi = { open: vi.fn(), setCapture: vi.fn() };
    const first = createHistoryRecoveryJournal(() => storage);
    const view = render(<ThemeProvider theme={theme}><SettingsView api={api} browserApi={browserApi}
      desktopApi={owner.api} recoveryJournal={first} /></ThemeProvider>);
    const dialog = await openConsent();
    fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
    fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' }));
    expect(first.getState()?.desired).toBe('running');
    expect(api.update).toHaveBeenCalledOnce();
    view.unmount();

    // A new journal instance simulates a fresh renderer loading persisted metadata.
    const restarted = createHistoryRecoveryJournal(() => storage);
    const nextApi = { get: vi.fn().mockResolvedValue(initial), update: vi.fn(), revoke: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={nextApi} browserApi={browserApi}
      desktopApi={owner.api} recoveryJournal={restarted} /></ThemeProvider>);
    expect(await screen.findByText("Your previous change wasn't confirmed. These are your current settings.")).toBeTruthy();
    // Nothing is replayed: history reads as off, and turning it on asks again.
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(historySwitch().checked).toBe(false);
    expect(historySwitch()).toHaveProperty('disabled', false);
    expect(nextApi.update).not.toHaveBeenCalled();
    await act(async () => complete(authorized));
    expect(historySwitch().checked).toBe(false);

    nextApi.get.mockResolvedValue(authorized);
    await act(async () => bridgeTransportStore.setAgent({ ...bridgeTransportStore.getState().agent!,
      required_history_settings_revision: 2, applied_history_settings_revision: 2 }));
    expect(await screen.findByRole('button', { name: 'Allow browser access' })).toHaveProperty('disabled', false);
    expect(restarted.getState()).toBeNull();
    expect(createHistoryRecoveryJournal(() => storage).getState()).toBeNull();
    expect(nextApi.update).not.toHaveBeenCalled();
    expect(browserApi.open).not.toHaveBeenCalled();
    expect(api.update).toHaveBeenCalledOnce();
  });

  it('does not repeat accepted consent when the first owner read after restart confirms it', async () => {
    connectExtension();
    const owner = desktopOwner();
    const storage = durableStorage();
    const first = createHistoryRecoveryJournal(() => storage);
    expect(first.claim(owner.api.getState().snapshot!.journey_id,
      { before: initial, desired: 'running', acceptConsent: true })).not.toBeNull();
    const api = { get: vi.fn(async () => authorized), update: vi.fn(), revoke: vi.fn() };
    const browserApi = { open: vi.fn(), setCapture: vi.fn() };
    const restarted = createHistoryRecoveryJournal(() => storage);
    render(<ThemeProvider theme={theme}><SettingsView api={api} desktopApi={owner.api}
      browserApi={browserApi} recoveryJournal={restarted} /></ThemeProvider>);
    expect((await screen.findByRole<HTMLInputElement>('switch', { name: 'Add older messages' })).checked).toBe(true);
    await waitFor(() => expect(restarted.getState()).toBeNull());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(api.update).not.toHaveBeenCalled();
    expect(browserApi.open).not.toHaveBeenCalled();
  });

  it('settles a committed revocation from the initial owner read after restart', async () => {
    connectExtension();
    const owner = desktopOwner();
    const storage = durableStorage();
    const first = createHistoryRecoveryJournal(() => storage);
    expect(first.claim(owner.api.getState().snapshot!.journey_id,
      { before: authorized, desired: 'revoked', acceptConsent: false })).not.toBeNull();
    const revoked: HistorySettings = { ...initial, settings_revision: 3,
      desired_state: 'revoked', effective_state: 'revoked' };
    const api = { get: vi.fn(async () => revoked), update: vi.fn(), revoke: vi.fn() };
    const restarted = createHistoryRecoveryJournal(() => storage);
    render(<ThemeProvider theme={theme}><SettingsView api={api} desktopApi={owner.api} recoveryJournal={restarted} /></ThemeProvider>);
    expect((await screen.findByRole<HTMLInputElement>('switch', { name: 'Add older messages' })).checked).toBe(false);
    await waitFor(() => expect(restarted.getState()).toBeNull());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.update).not.toHaveBeenCalled();
    expect(api.revoke).not.toHaveBeenCalled();
    expect(screen.queryByText("Your change hasn't been confirmed.")).toBeNull();
  });

  it.each(['policy', 'journey', 'account'] as const)('invalidates stored consent on a changed %s', async changed => {
    connectExtension();
    const owner = desktopOwner();
    const storage = durableStorage();
    const first = createHistoryRecoveryJournal(() => storage);
    expect(first.claim(owner.api.getState().snapshot!.journey_id,
      { before: initial, desired: 'running', acceptConsent: true })).not.toBeNull();
    let settings: HistorySettings = initial;
    if (changed === 'policy') settings = { ...initial, consent_policy_version: 'history-consent-v2' };
    if (changed === 'journey') owner.publish({ snapshot: { ...owner.api.getState().snapshot!,
      journey_id: '44444444-4444-4444-8444-444444444444' } });
    if (changed === 'account') {
      bridgeTransportStore.bindAccount('creator-2');
      bridgeTransportStore.setConnection('connected');
      const previous = bridgeTransportStore.getState().agent;
      bridgeTransportStore.setAgent({ creator_account_id: 'creator-2', status: 'connected',
        agent_installation_id: '90000000-0000-4000-8000-000000000002',
        connection_id: '90000000-0000-4000-8000-000000000003',
        required_config_revision: 'config-1', applied_config_revision: 'config-1',
        required_history_settings_revision: 1, applied_history_settings_revision: 1,
        last_heartbeat_at: initial.updated_at, degraded_reason: null, browser: previous?.browser ?? null });
      owner.readiness({ schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active',
        analysis_admission: 'admitted' }, 'creator-2');
      settings = { ...initial, creator_account_id: 'creator-2' };
    }
    const api = { get: vi.fn(async () => settings), update: vi.fn(), revoke: vi.fn() };
    const restarted = createHistoryRecoveryJournal(() => storage);
    render(<ThemeProvider theme={theme}><SettingsView api={api} desktopApi={owner.api} recoveryJournal={restarted} /></ThemeProvider>);
    await waitFor(() => expect(restarted.getState()).toBeNull());
    // The stored consent is dropped, not replayed: history reads as off and asking again starts unticked.
    const toggle = await screen.findByRole<HTMLInputElement>('switch', { name: 'Add older messages' });
    expect(toggle.checked).toBe(false);
    expect(toggle).toHaveProperty('disabled', false);
    expect(screen.queryByRole('dialog')).toBeNull();
    const dialog = await openConsent();
    expect(dialog.getByRole('checkbox', { name: CONSENT_LABEL })).toHaveProperty('checked', false);
    expect(dialog.getByRole('button', { name: 'Turn on message history' })).toHaveProperty('disabled', true);
    expect(api.update).not.toHaveBeenCalled();
    expect(api.revoke).not.toHaveBeenCalled();
  });

  it('does not send a consent request if its recovery record cannot be saved', async () => {
    connectExtension();
    const owner = desktopOwner();
    const storage = durableStorage();
    const journal = createHistoryRecoveryJournal(() => ({ ...storage,
      setItem: () => { throw Error('unavailable'); } }));
    const api = { get: vi.fn(async () => initial), update: vi.fn(), revoke: vi.fn() };
    render(<ThemeProvider theme={theme}><SettingsView api={api} desktopApi={owner.api} recoveryJournal={journal} /></ThemeProvider>);
    const dialog = await openConsent();
    fireEvent.click(dialog.getByRole('checkbox', { name: CONSENT_LABEL }));
    await act(async () => fireEvent.click(dialog.getByRole('button', { name: 'Turn on message history' })));
    expect(consentDialog().getByText('Dipsy could not save the request. No change was sent.')).toBeTruthy();
    expect(api.update).not.toHaveBeenCalled();
    expect(journal.getState()).toBeNull();
  });
});
