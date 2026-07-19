import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { AppShell } from '../src/layouts/AppShell';
import type { HistorySettingsApi } from '../src/services/historySettingsApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';
import SettingsView from '../src/views/SettingsView';

async function expectNoCriticalOrSeriousViolations(root: Element) {
  const result = await axe.run(root, {
    resultTypes: ['violations'],
    // jsdom has no canvas/layout engine; production contrast is covered by the browser visual gate.
    rules: { 'color-contrast': { enabled: false } },
  });
  const severe = result.violations
    .filter(({ impact }) => impact === 'critical' || impact === 'serious')
    .map(({ description, id, nodes }) => ({
      description,
      id,
      targets: nodes.map(({ target }) => target.join(' ')),
    }));
  expect(severe).toEqual([]);
}

beforeEach(() => {
  useUserStore.getState().actions.setUserRole('creator-ceo');
});

afterEach(() => {
  cleanup();
  bridgeTransportStore.reset();
  useUserStore.getState().actions.setUserRole(null);
});

describe('critical accessibility gates', () => {
  it('keeps the AppBar status and application navigation free of serious axe findings', async () => {
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<h1>Dashboard workspace</h1>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ThemeProvider>,
    );

    expect(screen.getByRole('main')).toBeTruthy();
    await expectNoCriticalOrSeriousViolations(document.body);
  });

  it('keeps creator consent controls and status copy free of serious axe findings', async () => {
    const api: HistorySettingsApi = {
      get: async () => ({
        creator_account_id: 'creator-1',
        settings_revision: 2,
        consent_policy_version: 'history-consent-v1',
        consent_revision: 'consent-1',
        authorized_platform_creator_id: 'platform-creator-1',
        desired_state: 'running',
        effective_state: 'running',
        effective_config_revision: 'config-2',
        recent_window_days: 30,
        page_size: 50,
        pages_per_wake: 2,
        request_interval_ms: 1000,
        retry_limit: 3,
        updated_at: '2026-07-19T12:00:00Z',
      }),
      update: async () => { throw new Error('not used'); },
      revoke: async () => { throw new Error('not used'); },
    };
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <SettingsView api={api} />
      </ThemeProvider>,
    );

    await screen.findByRole('heading', { name: 'Historical message sync' });
    await expectNoCriticalOrSeriousViolations(document.body);
  });
});
