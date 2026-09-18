import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AnalyticsMetric,
  AnalyticsView,
  ConversationSummary,
  HistoricalCoverage,
  ProjectionState,
  StateSnapshotPayload,
} from '../src/protocol';
import type { CapabilityLicenseApi, CapabilityLicenseReadiness } from '../src/services/capabilityLicenseApi';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';
import CreatorDashboardView from '../src/views/CreatorDashboardView';

const ACCOUNT_ID = 'creator-account';
const AS_OF = '2026-07-19T12:00:00Z';

function metric(
  value: number | null,
  basis: AnalyticsMetric['basis'] = 'complete',
  sampleSize = value ?? 0,
  projectionRevision = 4,
): AnalyticsMetric {
  return {
    value,
    basis,
    observed_range: { start: '2026-07-01T00:00:00Z', end: AS_OF },
    complete_range:
      basis === 'complete'
        ? { start: '2026-07-01T00:00:00Z', end: AS_OF }
        : null,
    sample_size: sampleSize,
    as_of: AS_OF,
    projection_revision: projectionRevision,
  };
}

function analytics(
  values: [number | null, number | null, number | null, number | null],
  basis: AnalyticsMetric['basis'] = 'complete',
  projectionRevision = 4,
): AnalyticsView {
  return {
    total_conversations: metric(values[0], basis, 7, projectionRevision),
    total_messages: metric(values[1], basis, 19, projectionRevision),
    inbound_messages: metric(values[2], basis, 11, projectionRevision),
    outbound_messages: metric(values[3], basis, 8, projectionRevision),
  };
}

function conversation(conversationId: string): ConversationSummary {
  return {
    conversation_id: conversationId,
    platform_user_id: `fan-${conversationId}`,
    display_name: 'Alpha Fan',
    unread_count: 0,
    last_message_at: AS_OF,
    latest_message: {
      message_id: 'preview-only',
      text: 'Bounded latest preview',
      sent_at: AS_OF,
      direction: 'inbound',
      sentiment: 'positive',
    },
    coverage: {
      status: 'complete',
      boundary: 'history_start',
      earliest_available_at: '2026-07-01T00:00:00Z',
      latest_acquired_at: AS_OF,
      data_as_of: AS_OF,
      reason_code: null,
    },
  };
}

const COMPLETE_COVERAGE: HistoricalCoverage = {
  status: 'complete',
  phase: 'complete',
  generation_id: '90000000-0000-4000-8000-000000000001',
  as_of: AS_OF,
  discovered_conversations: 1,
  complete_conversations: 1,
  complete_as_of: AS_OF,
  reason: null,
};

const CURRENT_PROJECTION: ProjectionState = {
  status: 'current',
  canonical_revision: 4,
  projected_revision: 4,
  projected_at: AS_OF,
  reason: null,
};

function snapshot({
  analyticsView = analytics([1, 19, 11, 8]),
  coverage = COMPLETE_COVERAGE,
  projection = CURRENT_PROJECTION,
  viewRevision = 1,
}: {
  analyticsView?: AnalyticsView;
  coverage?: HistoricalCoverage;
  projection?: ProjectionState;
  viewRevision?: number;
} = {}): StateSnapshotPayload {
  return {
    creator_account_id: ACCOUNT_ID,
    view_revision: viewRevision,
    generated_at: AS_OF,
    conversations: [conversation('alpha')],
    analytics: analyticsView,
    coverage,
    projection,
    live_freshness: {
      status: 'current',
      last_observed_at: AS_OF,
      last_committed_at: AS_OF,
      expires_at: '2026-07-19T12:02:00Z',
      pending_count: 0,
      reason: null,
    },
  };
}

function readyStore(payload = snapshot()) {
  const store = createBridgeTransportStore();
  store.bindAccount(ACCOUNT_ID);
  store.setConnection('connected');
  store.setAgent({
    creator_account_id: ACCOUNT_ID,
    status: 'connected',
    agent_installation_id: '20000000-0000-4000-8000-000000000001',
    connection_id: '10000000-0000-4000-8000-000000000001',
    required_config_revision: 'config-4',
    applied_config_revision: 'config-4',
    required_history_settings_revision: 9,
    applied_history_settings_revision: 9,
    last_heartbeat_at: AS_OF,
    degraded_reason: null,
  });
  store.applySnapshot(payload);
  return store;
}

function readinessApi(
  ready: boolean | null,
  readiness?: CapabilityLicenseApi['readiness'],
): CapabilityLicenseApi {
  return {
    readiness: vi.fn(readiness ?? (async () => {
      if (ready === null) throw new Error('Readiness unavailable.');
      return {
        schema: 'ofca-analysis-readiness/v1' as const,
        commercial_authority: ready ? 'active' as const : 'required' as const,
        analysis_admission: ready ? 'admitted' as const : 'blocked' as const,
      };
    })),
    redeem: vi.fn(async () => {
      throw new Error('not used');
    }),
  };
}

function mountDashboard(
  store: ReturnType<typeof createBridgeTransportStore>,
  activationApi: CapabilityLicenseApi,
) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      <MemoryRouter>
        <CreatorDashboardView activationApi={activationApi} store={store} />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

/** Mounts the dashboard and lets the readiness request settle. */
async function renderDashboard(
  store: ReturnType<typeof createBridgeTransportStore>,
  activationApi = readinessApi(null),
) {
  const view = mountDashboard(store, activationApi);
  await act(async () => {
    await vi.mocked(activationApi.readiness).mock.results.at(-1)?.value.catch(() => undefined);
  });
  return view;
}

function overview() {
  return within(screen.getByRole('region', { name: 'Overview' }));
}

function expectStat(label: 'Conversations' | 'Messages', value: string) {
  const stat = within(overview().getByRole('group', { name: label }));
  expect(stat.getByText(value, { exact: true })).toBeTruthy();
}

function expectSplit(label: 'Received' | 'Sent', value: string) {
  const legend = overview().getByText(label, { exact: true }).parentElement!;
  expect(within(legend).getByText(value, { exact: true })).toBeTruthy();
}

beforeEach(() => useUserStore.getState().actions.setUserRole('creator-ceo'));

afterEach(() => {
  cleanup();
  useUserStore.getState().actions.setUserRole(null);
});

describe('CreatorDashboardView', () => {
  it('shows only the processing status and no alert before the first snapshot', async () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);

    await renderDashboard(store);

    expect(screen.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('Processing your data…');
    expect(screen.queryByRole('region', { name: 'Overview' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Recent conversations' })).toBeNull();
  });

  it('marks synced-subset counts and shows sync progress instead of a partial-basis sentence', async () => {
    const store = readyStore(
      snapshot({
        analyticsView: analytics([7, 0, null, 4], 'synced_subset'),
        coverage: {
          ...COMPLETE_COVERAGE,
          status: 'partial',
          phase: 'backfilling',
          discovered_conversations: 4,
          complete_conversations: 2,
          complete_as_of: null,
          reason: 'conversation_evidence_missing',
        },
      }),
    );

    await renderDashboard(store);

    expectStat('Conversations', '7+');
    expectStat('Messages', 'None yet');
    expectSplit('Received', '—');
    expectSplit('Sent', '4+');
    expect(overview().getByRole('progressbar', { name: 'History 50% synced' })).toBeTruthy();
    expect(screen.queryByText(/Counts include messages synced so far/)).toBeNull();
    expect(screen.queryByText(/Based on your full message history/)).toBeNull();
  });

  it('names the partial basis when history sync is not in progress', async () => {
    const store = readyStore(
      snapshot({
        analyticsView: analytics([7, 12, 5, 7], 'synced_subset'),
        coverage: {
          ...COMPLETE_COVERAGE,
          status: 'partial',
          phase: 'paused',
          complete_as_of: null,
        },
      }),
    );

    await renderDashboard(store);

    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(screen.getByText(/Counts include messages synced so far/)).toBeTruthy();
  });

  it('renders complete counts with the basis details in a popover', async () => {
    const store = readyStore();

    await renderDashboard(store);

    expectStat('Conversations', '1');
    expectStat('Messages', '19');
    expectSplit('Received', '11');
    expectSplit('Sent', '8');
    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText(/Based on your full message history/)).toBeTruthy();

    const details = screen.getByRole('button', { name: 'Details' });
    expect(details.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Messages counted', { exact: true })).toBeNull();
    fireEvent.click(details);
    expect(details.getAttribute('aria-expanded')).toBe('true');
    const popover = within(screen.getByRole('dialog', { name: 'How these numbers are counted' }));
    const counted = popover.getByText('Messages counted', { exact: true }).nextElementSibling;
    expect(counted?.textContent).toBe('19');
    expect(popover.queryByText('Data version')).toBeNull();
  });

  it('lists recent conversations and links to the inbox only for viewers who can open it', async () => {
    const store = readyStore();
    const view = await renderDashboard(store);

    const recent = within(screen.getByRole('region', { name: 'Recent conversations' }));
    expect(recent.getByText('Alpha Fan')).toBeTruthy();
    expect(recent.getByText('Bounded latest preview')).toBeTruthy();
    expect(recent.getByRole('link', { name: 'Open inbox' }).getAttribute('href')).toBe('/inbox');

    view.unmount();
    useUserStore.getState().actions.setUserRole(null);
    await renderDashboard(store);
    expect(screen.getByRole('region', { name: 'Recent conversations' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open inbox' })).toBeNull();
  });

  it('replaces the numbers with the setup prompt until history sync starts', async () => {
    const store = readyStore(
      snapshot({
        analyticsView: analytics([0, 0, 0, 0], 'synced_subset'),
        coverage: {
          ...COMPLETE_COVERAGE,
          status: 'partial',
          phase: 'not_started',
          discovered_conversations: null,
          complete_conversations: 0,
          complete_as_of: null,
        },
      }),
    );

    await renderDashboard(store);

    const prompt = within(screen.getByRole('region', { name: 'Finish setup' }));
    expect(prompt.getByText('1 of 3 complete')).toBeTruthy();
    expect(prompt.getByText('Connect the browser extension').textContent).toContain('(done)');
    expect(prompt.getByText('Turn on message history').textContent).not.toContain('(done)');
    expect(prompt.getByText('Turn on Full analytics').textContent).not.toContain('(done)');
    const current = prompt.getAllByRole('listitem').filter((item) => item.getAttribute('aria-current') === 'step');
    expect(current.map((item) => item.textContent)).toEqual(['2Turn on message history']);
    expect(prompt.getByRole('link', { name: 'Continue setup' }).getAttribute('href')).toBe(
      '/settings',
    );
    expect(screen.queryByRole('region', { name: 'Overview' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Recent conversations' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('applies the next analytics envelope and projection revision atomically', async () => {
    const store = readyStore();
    await renderDashboard(store);
    expectStat('Messages', '19');

    act(() => {
      expect(
        store.applyDelta({
          creator_account_id: ACCOUNT_ID,
          view_revision: 2,
          committed_at: '2026-07-19T12:01:00Z',
          changes: [
            {
              type: 'analytics.replace',
              analytics: analytics([1, 20, 11, 9], 'complete', 5),
            },
            {
              type: 'projection.replace',
              projection: {
                ...CURRENT_PROJECTION,
                canonical_revision: 5,
                projected_revision: 5,
                projected_at: '2026-07-19T12:01:00Z',
              },
            },
          ],
        }),
      ).toBe('applied');
    });

    expectStat('Messages', '20');
    expectSplit('Sent', '9');
  });

  it('withholds counts when the projection is unavailable', async () => {
    const store = readyStore(
      snapshot({
        projection: {
          ...CURRENT_PROJECTION,
          status: 'unavailable',
          reason: 'projection_generation_failed',
        },
      }),
    );

    await renderDashboard(store);

    expectStat('Conversations', '—');
    expectStat('Messages', '—');
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Numbers unavailable');
    expect(alert.textContent).not.toContain('projection_generation_failed');
  });

  it('keeps the last counts through resync and disconnect', async () => {
    const store = readyStore();
    await renderDashboard(store);
    expect(screen.queryByRole('alert')).toBeNull();

    act(() => store.beginResync());
    expect(screen.getByRole('alert').textContent).toContain('Refreshing your numbers');
    expectStat('Messages', '19');

    act(() => store.markDisconnected());
    expect(screen.getByRole('alert').textContent).toContain('Updates paused');
    expectStat('Messages', '19');
  });

  it('replaces a raw coverage reason code with friendly text when history sync is blocked', async () => {
    const store = readyStore(
      snapshot({
        coverage: {
          ...COMPLETE_COVERAGE,
          status: 'partial',
          phase: 'blocked',
          complete_as_of: null,
          reason: 'consent_revoked',
        },
      }),
    );

    await renderDashboard(store);

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Message history sync was turned off.');
    expect(alert.textContent).not.toContain('consent_revoked');
  });

  it('falls back to a generic message for an unrecognized coverage reason code', async () => {
    const store = readyStore(
      snapshot({
        coverage: {
          ...COMPLETE_COVERAGE,
          status: 'partial',
          phase: 'blocked',
          complete_as_of: null,
          reason: 'agent_reported_a_new_code',
        },
      }),
    );

    await renderDashboard(store);

    const alert = screen.getByRole('alert');
    expect(alert.textContent).not.toContain('agent_reported_a_new_code');
    expect(alert.textContent).toContain('This needs attention');
  });

  it('renders the setup prompt and the overview together once readiness settles', async () => {
    let resolve!: (readiness: CapabilityLicenseReadiness) => void;
    const api = readinessApi(null, () => new Promise((settle) => { resolve = settle; }));
    mountDashboard(readyStore(), api);

    expect(screen.getByRole('status').textContent).toBe('Processing your data…');
    expect(screen.queryByRole('region', { name: 'Overview' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Finish setup' })).toBeNull();

    await act(async () => resolve({
      schema: 'ofca-analysis-readiness/v1',
      commercial_authority: 'required',
      analysis_admission: 'blocked',
    }));

    const prompt = screen.getByRole('region', { name: 'Finish setup' });
    const overview = screen.getByRole('region', { name: 'Overview' });
    expect(prompt.compareDocumentPosition(overview) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText('Processing your data…')).toBeNull();
    expectStat('Messages', '19');
  });

  it('renders the settled layout at once when the dashboard mounts again', async () => {
    const store = readyStore();
    const api = readinessApi(false);
    const first = await renderDashboard(store, api);
    first.unmount();

    mountDashboard(store, api);

    expect(screen.getByRole('region', { name: 'Finish setup' })).toBeTruthy();
    expectStat('Messages', '19');
  });

  it('stops waiting for readiness after the time limit', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      const api = readinessApi(null, () => new Promise(() => undefined));
      mountDashboard(readyStore(), api);
      expect(screen.queryByRole('region', { name: 'Overview' })).toBeNull();

      act(() => vi.advanceTimersByTime(3000));

      expect(screen.getByRole('region', { name: 'Overview' })).toBeTruthy();
      expect(screen.queryByRole('region', { name: 'Finish setup' })).toBeNull();
      expect(vi.mocked(api.readiness).mock.calls[0]?.[0]?.aborted).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});