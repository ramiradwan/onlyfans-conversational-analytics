import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HistorySettings } from '../src/protocol';
import type { HistorySettingsApi } from '../src/services/historySettingsApi';
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
  useUserStore.getState().actions.setUserRole(null);
  bridgeTransportStore.reset();
});

function connectExtension() {
  bridgeTransportStore.bindAccount('creator-1');
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
    expect(screen.getByText('Pausing when the browser extension next connects.')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.queryByText('Pausing when the browser extension next connects.')).toBeNull();
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
    expect(api.update).toHaveBeenLastCalledWith(3, expect.objectContaining({ desired_state: 'running' }));
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
    );
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeTruthy();
    expect(screen.queryByText('platform-creator-1')).toBeNull();
    expect(screen.queryByText('history-consent-v1')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }));
    expect(api.revoke).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Turn off' }));
    await waitFor(() => expect(api.revoke).toHaveBeenCalledWith(2));
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
});
