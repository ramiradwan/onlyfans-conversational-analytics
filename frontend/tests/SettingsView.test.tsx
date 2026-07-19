import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  useUserStore.getState().actions.setUserRole(null);
  bridgeTransportStore.reset();
});

describe('SettingsView history consent', () => {
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

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <SettingsView api={api} />
      </ThemeProvider>,
    );

    const start = await screen.findByRole('button', { name: 'Start historical sync' });
    expect(start.hasAttribute('disabled')).toBe(true);
    fireEvent.click(
      screen.getByRole('checkbox', {
        name: /I authorize read-only local historical sync/,
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Start historical sync' }));

    await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1));
    expect(api.update).toHaveBeenCalledWith(
      1,
      expect.objectContaining({
        desired_state: 'running',
        accept_consent: true,
        consent_policy_version: 'history-consent-v1',
      }),
    );
    expect(await screen.findByRole('button', { name: 'Pause sync' })).toBeTruthy();
    expect(screen.getByText('platform-creator-1')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Revoke consent' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Revoke consent' }));
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
    expect(await screen.findByText(/available to the creator account owner/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Start historical sync' })).toBeNull();
  });
});
