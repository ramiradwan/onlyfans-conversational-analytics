/**
 * axe-core checks for critical and serious findings on production layouts, views, and the
 * application gate. jsdom has no layout engine, so `color-contrast` is disabled here; theme
 * generation validates token contrast ratios.
 */
import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { adaptAnalyticsReadModel, classifyAnalyticsModel } from '../src/analytics';
import { AppShell } from '../src/layouts/AppShell';
import type { CapabilityLicenseApi } from '../src/services/capabilityLicenseApi';
import type { CompanionPairingApi } from '../src/services/companionPairingApi';
import type { CreatorVaultApi } from '../src/services/creatorVaultApi';
import type { HistorySettingsApi } from '../src/services/historySettingsApi';
import type { MessageApi } from '../src/services/messageApi';
import { analyticsStore } from '../src/store/analyticsStore';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';
import AnalyticsView from '../src/views/AnalyticsView';
import CreatorDashboardView from '../src/views/CreatorDashboardView';
import GraphExplorerView from '../src/views/GraphExplorerView';
import OperatorInboxView from '../src/views/OperatorInboxView';
import SettingsView from '../src/views/SettingsView';
import SettingsWithVaultView from '../src/views/SettingsWithVaultView';
import { WebAuthnAccessView } from '../src/views/WebAuthnAccessView';
import { analyticsUpdateFixture } from './analyticsFixture';

async function expectNoCriticalOrSeriousViolations(root: Element) {
  const result = await axe.run(root, {
    resultTypes: ['violations'],
    // jsdom has no layout engine; theme generation validates token contrast.
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
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  });
  useUserStore.getState().actions.setUserRole('creator-ceo');
});

afterEach(() => {
  cleanup();
  bridgeTransportStore.reset();
  analyticsStore.setState({
    state: {
      status: 'loading',
      data: null,
      isRefreshing: false,
      message: 'Loading canonical analytics…',
    },
    dateRange: { startDate: '', endDate: '' },
  });
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

  it('keeps CreatorDashboardView free of serious axe findings when populated with realistic snapshot state', async () => {
    bridgeTransportStore.bindAccount('creator-1');
    bridgeTransportStore.setConnection('connected');
    bridgeTransportStore.setAgent({
      creator_account_id: 'creator-1',
      status: 'connected',
      agent_installation_id: '20000000-0000-4000-8000-000000000001',
      connection_id: '10000000-0000-4000-8000-000000000001',
      required_config_revision: 'config-4',
      applied_config_revision: 'config-4',
      required_history_settings_revision: 9,
      applied_history_settings_revision: 9,
      last_heartbeat_at: '2026-07-19T12:00:00Z',
      degraded_reason: null,
    });
    bridgeTransportStore.applySnapshot({
      creator_account_id: 'creator-1',
      view_revision: 1,
      generated_at: '2026-07-19T12:00:00Z',
      conversations: [
        {
          conversation_id: 'conv-1',
          platform_user_id: 'fan-1',
          display_name: 'Alpha Fan',
          unread_count: 0,
          last_message_at: '2026-07-19T12:00:00Z',
          latest_message: {
            message_id: 'msg-1',
            text: 'Bounded latest preview',
            sent_at: '2026-07-19T12:00:00Z',
            direction: 'inbound',
            sentiment: 'positive',
          },
          coverage: {
            status: 'complete',
            boundary: 'history_start',
            earliest_available_at: '2026-07-01T00:00:00Z',
            latest_acquired_at: '2026-07-19T12:00:00Z',
            data_as_of: '2026-07-19T12:00:00Z',
            reason_code: null,
          },
        },
      ],
      analytics: {
        total_conversations: {
          value: 1,
          basis: 'complete',
          observed_range: { start: '2026-07-01T00:00:00Z', end: '2026-07-19T12:00:00Z' },
          complete_range: { start: '2026-07-01T00:00:00Z', end: '2026-07-19T12:00:00Z' },
          sample_size: 1,
          as_of: '2026-07-19T12:00:00Z',
          projection_revision: 4,
        },
        total_messages: {
          value: 19,
          basis: 'complete',
          observed_range: { start: '2026-07-01T00:00:00Z', end: '2026-07-19T12:00:00Z' },
          complete_range: { start: '2026-07-01T00:00:00Z', end: '2026-07-19T12:00:00Z' },
          sample_size: 19,
          as_of: '2026-07-19T12:00:00Z',
          projection_revision: 4,
        },
        inbound_messages: {
          value: 11,
          basis: 'complete',
          observed_range: { start: '2026-07-01T00:00:00Z', end: '2026-07-19T12:00:00Z' },
          complete_range: { start: '2026-07-01T00:00:00Z', end: '2026-07-19T12:00:00Z' },
          sample_size: 11,
          as_of: '2026-07-19T12:00:00Z',
          projection_revision: 4,
        },
        outbound_messages: {
          value: 8,
          basis: 'complete',
          observed_range: { start: '2026-07-01T00:00:00Z', end: '2026-07-19T12:00:00Z' },
          complete_range: { start: '2026-07-01T00:00:00Z', end: '2026-07-19T12:00:00Z' },
          sample_size: 8,
          as_of: '2026-07-19T12:00:00Z',
          projection_revision: 4,
        },
      },
      coverage: {
        status: 'complete',
        phase: 'complete',
        generation_id: '90000000-0000-4000-8000-000000000001',
        as_of: '2026-07-19T12:00:00Z',
        discovered_conversations: 1,
        complete_conversations: 1,
        complete_as_of: '2026-07-19T12:00:00Z',
        reason: null,
      },
      projection: {
        status: 'current',
        canonical_revision: 4,
        projected_revision: 4,
        projected_at: '2026-07-19T12:00:00Z',
        reason: null,
      },
      live_freshness: {
        status: 'current',
        last_observed_at: '2026-07-19T12:00:00Z',
        last_committed_at: '2026-07-19T12:00:00Z',
        expires_at: '2026-07-19T12:02:00Z',
        pending_count: 0,
        reason: null,
      },
    });

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <CreatorDashboardView />
      </ThemeProvider>,
    );

    expect(screen.getByRole('heading', { name: 'Creator dashboard' })).toBeTruthy();
    await expectNoCriticalOrSeriousViolations(document.body);
  });

  it('keeps OperatorInboxView free of serious axe findings when rendered with realistic transport state', async () => {
    useUserStore.getState().actions.setUserRole('operator');
    bridgeTransportStore.bindAccount('creator-1');
    bridgeTransportStore.applySnapshot({
      creator_account_id: 'creator-1',
      view_revision: 1,
      generated_at: '2026-07-18T13:00:00Z',
      conversations: [
        {
          conversation_id: 'conv-1',
          platform_user_id: 'fan-1',
          display_name: 'Alpha Fan',
          unread_count: 1,
          last_message_at: '2026-07-18T13:00:00Z',
          latest_message: {
            message_id: 'msg-1',
            text: 'Hello from fan',
            sent_at: '2026-07-18T13:00:00Z',
            direction: 'inbound',
            sentiment: 'neutral',
          },
          coverage: {
            status: 'complete',
            boundary: 'history_start',
            earliest_available_at: '2026-01-01T00:00:00Z',
            latest_acquired_at: '2026-07-18T13:00:00Z',
            data_as_of: '2026-07-18T13:00:00Z',
            reason_code: null,
          },
        },
      ],
      analytics: {
        total_conversations: {
          value: 1,
          basis: 'synced_subset',
          observed_range: { start: null, end: '2026-07-18T13:00:00Z' },
          complete_range: null,
          sample_size: 1,
          as_of: '2026-07-18T13:00:00Z',
          projection_revision: 7,
        },
        total_messages: {
          value: 2,
          basis: 'synced_subset',
          observed_range: { start: null, end: '2026-07-18T13:00:00Z' },
          complete_range: null,
          sample_size: 2,
          as_of: '2026-07-18T13:00:00Z',
          projection_revision: 7,
        },
        inbound_messages: {
          value: 1,
          basis: 'synced_subset',
          observed_range: { start: null, end: '2026-07-18T13:00:00Z' },
          complete_range: null,
          sample_size: 1,
          as_of: '2026-07-18T13:00:00Z',
          projection_revision: 7,
        },
        outbound_messages: {
          value: 1,
          basis: 'synced_subset',
          observed_range: { start: null, end: '2026-07-18T13:00:00Z' },
          complete_range: null,
          sample_size: 1,
          as_of: '2026-07-18T13:00:00Z',
          projection_revision: 7,
        },
      },
      coverage: {
        status: 'complete',
        phase: 'complete',
        generation_id: '90000000-0000-4000-8000-000000000001',
        as_of: '2026-07-18T13:00:00Z',
        discovered_conversations: 1,
        complete_conversations: 1,
        complete_as_of: '2026-07-18T13:00:00Z',
        reason: null,
      },
      projection: {
        status: 'current',
        canonical_revision: 7,
        projected_revision: 7,
        projected_at: '2026-07-18T13:00:00Z',
        reason: null,
      },
      live_freshness: {
        status: 'current',
        last_observed_at: '2026-07-18T13:00:00Z',
        last_committed_at: '2026-07-18T13:00:00Z',
        expires_at: '2026-07-18T13:02:00Z',
        pending_count: 0,
        reason: null,
      },
    });

    const mockMessageApi: MessageApi = {
      getPage: vi.fn(async () => ({
        creator_account_id: 'creator-1',
        conversation_id: 'conv-1',
        projection_generation: 'projection-generation-7',
        read_revision: 7,
        generated_at: '2026-07-18T13:00:00Z',
        items: [
          {
            message_id: 'msg-1',
            text: 'Hello from fan',
            sent_at: '2026-07-18T13:00:00Z',
            direction: 'inbound',
            sentiment: 'neutral',
          },
          {
            message_id: 'msg-2',
            text: 'Hello back',
            sent_at: '2026-07-18T13:01:00Z',
            direction: 'outbound',
            sentiment: 'positive',
          },
        ],
        older_cursor: null,
        has_older_stored_items: false,
        conversation_coverage: {
          status: 'complete',
          boundary: 'history_start',
          earliest_available_at: '2026-01-01T00:00:00Z',
          latest_acquired_at: '2026-07-18T13:00:00Z',
          data_as_of: '2026-07-18T13:00:00Z',
          reason_code: null,
        },
        projection: {
          status: 'current',
          canonical_revision: 7,
          projected_revision: 7,
          projected_at: '2026-07-18T13:00:00Z',
          reason: null,
        },
      })),
    };

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <OperatorInboxView messageApi={mockMessageApi} />
      </ThemeProvider>,
    );

    await screen.findByRole('heading', { name: 'Alpha Fan' });
    await expectNoCriticalOrSeriousViolations(document.body);
  });

  it('keeps AnalyticsView free of serious axe findings when rendered with active analytics read state', async () => {
    const activeState = classifyAnalyticsModel(adaptAnalyticsReadModel(analyticsUpdateFixture()));
    analyticsStore.setState({ state: activeState });

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <AnalyticsView />
      </ThemeProvider>,
    );

    expect(screen.getByRole('heading', { name: 'Analytics' })).toBeTruthy();
    await expectNoCriticalOrSeriousViolations(document.body);
  });

  it('keeps GraphExplorerView free of serious axe findings when rendered with local projection state', async () => {
    bridgeTransportStore.bindAccount('creator-1');
    bridgeTransportStore.applySnapshot({
      creator_account_id: 'creator-1',
      view_revision: 1,
      generated_at: '2026-07-19T12:00:00Z',
      conversations: [],
      analytics: {
        total_conversations: {
          value: 0,
          basis: 'complete',
          observed_range: { start: null, end: null },
          complete_range: { start: null, end: null },
          sample_size: 0,
          as_of: '2026-07-19T12:00:00Z',
          projection_revision: 2,
        },
        total_messages: {
          value: 0,
          basis: 'complete',
          observed_range: { start: null, end: null },
          complete_range: { start: null, end: null },
          sample_size: 0,
          as_of: '2026-07-19T12:00:00Z',
          projection_revision: 2,
        },
        inbound_messages: {
          value: 0,
          basis: 'complete',
          observed_range: { start: null, end: null },
          complete_range: { start: null, end: null },
          sample_size: 0,
          as_of: '2026-07-19T12:00:00Z',
          projection_revision: 2,
        },
        outbound_messages: {
          value: 0,
          basis: 'complete',
          observed_range: { start: null, end: null },
          complete_range: { start: null, end: null },
          sample_size: 0,
          as_of: '2026-07-19T12:00:00Z',
          projection_revision: 2,
        },
      },
      coverage: {
        status: 'complete',
        phase: 'complete',
        generation_id: '90000000-0000-4000-8000-000000000001',
        as_of: '2026-07-19T12:00:00Z',
        discovered_conversations: 0,
        complete_conversations: 0,
        complete_as_of: '2026-07-19T12:00:00Z',
        reason: null,
      },
      projection: {
        status: 'current',
        canonical_revision: 2,
        projected_revision: 2,
        projected_at: '2026-07-19T12:00:00Z',
        reason: null,
      },
      live_freshness: {
        status: 'current',
        last_observed_at: '2026-07-19T12:00:00Z',
        last_committed_at: '2026-07-19T12:00:00Z',
        expires_at: '2026-07-19T12:02:00Z',
        pending_count: 0,
        reason: null,
      },
    });

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <GraphExplorerView />
      </ThemeProvider>,
    );

    expect(screen.getByRole('heading', { name: 'Graph explorer' })).toBeTruthy();
    await expectNoCriticalOrSeriousViolations(document.body);
  });

  it('keeps SettingsWithVaultView free of serious axe findings for composite production view', async () => {
    bridgeTransportStore.bindAccount('creator-1');
    const historyApi: HistorySettingsApi = {
      get: vi.fn(async () => ({
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
      })),
      update: vi.fn(async () => { throw new Error('not used'); }),
      revoke: vi.fn(async () => { throw new Error('not used'); }),
    };

    const vaultApi: CreatorVaultApi = {
      get: vi.fn(async () => ({
        creator_account_id: 'creator-1',
        policy: {
          enabled: true,
          policy_type: 'finite',
          finite_horizon_days: 365,
          revision: 1,
        },
        capabilities: {
          finite_retention: true,
          indefinite_retention: false,
          deletion_scopes: ['message', 'conversation', 'participant', 'all'],
          unlink_archive_treatments: ['preserve', 'delete'],
          export: true,
        },
      })),
      command: vi.fn(async () => { throw new Error('not used'); }),
      exportDocument: vi.fn(async () => { throw new Error('not used'); }),
    };

    const pairingApi: CompanionPairingApi = {
      pins: vi.fn(async () => []),
      revoke: vi.fn(async () => { throw new Error('not used'); }),
      open: vi.fn(async () => { throw new Error('not used'); }),
      get: vi.fn(async () => { throw new Error('not used'); }),
      change: vi.fn(async () => { throw new Error('not used'); }),
    };

    const activationApi: CapabilityLicenseApi = {
      readiness: vi.fn(async () => ({
        schema: 'ofca-analysis-readiness/v1' as const,
        commercial_authority: 'required' as const,
        analysis_admission: 'blocked' as const,
      })),
      redeem: vi.fn(async () => { throw new Error('not used'); }),
    };

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <SettingsWithVaultView
          historyApi={historyApi}
          pairingApi={pairingApi}
          activationApi={activationApi}
          vaultApi={vaultApi}
        />
      </ThemeProvider>,
    );

    await screen.findByRole('heading', { name: 'Historical message sync' });
    await screen.findByRole('heading', { name: 'Connect browser extension' });
    await screen.findByRole('button', { name: 'Open connection window' });
    await screen.findByRole('heading', { name: 'Full activation required' });
    await screen.findByRole('heading', { name: 'Creator Vault' });
    await expectNoCriticalOrSeriousViolations(document.body);
  });

  it('keeps WebAuthnAccessView at the application gate free of serious axe findings in resting state', async () => {
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <WebAuthnAccessView />
      </ThemeProvider>,
    );

    expect(screen.getByRole('heading', { name: 'Secure your Bridge' })).toBeTruthy();
    await expectNoCriticalOrSeriousViolations(document.body);
  });
});

