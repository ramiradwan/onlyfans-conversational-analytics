import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  AnalyticsMetric,
  AnalyticsView,
  ConversationSummary,
  HistoricalCoverage,
  ProjectionState,
  StateSnapshotPayload,
} from '../src/protocol';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';
import CreatorDashboardView from '../src/views/CreatorDashboardView';

vi.mock('../src/components/KpiCard', () => ({
  KpiCard: ({
    detail,
    isLoading,
    title,
    value,
  }: {
    detail?: string;
    isLoading?: boolean;
    title: string;
    value: number | string;
  }) => (
    <section aria-label={`${title} metric`}>
      {isLoading ? (
        `Loading ${title}`
      ) : (
        <>
          <span>{title}</span>
          <strong>{value}</strong>
          {detail && <small>{detail}</small>}
        </>
      )}
    </section>
  ),
}));

vi.mock('@mui/x-charts/LineChart', () => ({
  LineChart: ({ 'aria-label': ariaLabel }: { 'aria-label'?: string }) => (
    <div aria-label={ariaLabel} role="img" />
  ),
}));

vi.mock('@mui/x-charts/PieChart', () => ({
  PieChart: ({ 'aria-label': ariaLabel }: { 'aria-label'?: string }) => (
    <div aria-label={ariaLabel} role="img" />
  ),
}));

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

function renderDashboard(store: ReturnType<typeof createBridgeTransportStore>) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      <CreatorDashboardView store={store} />
    </ThemeProvider>,
  );
}

function expectKpi(label: string, value: string) {
  const card = screen.getByLabelText(`${label} metric`);
  expect(within(card).getByText(label, { exact: true })).toBeTruthy();
  expect(within(card).getByText(value, { exact: true })).toBeTruthy();
}

afterEach(() => cleanup());

describe('CreatorDashboardView v2 analytics evidence', () => {
  it('renders a truthful loading state before the first bounded snapshot', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);

    renderDashboard(store);

    expect(screen.getByRole('heading', { name: 'Creator dashboard' })).toBeTruthy();
    expect(screen.getByText('Connecting', { exact: true })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toContain(
      'Loading dashboard. Waiting for the first analytics snapshot…',
    );
    expect(screen.getByLabelText('Loading active conversations')).toBeTruthy();
  });

  it('qualifies synchronized-subset counts, including zero and unavailable values', () => {
    const partialCoverage: HistoricalCoverage = {
      ...COMPLETE_COVERAGE,
      status: 'partial',
      phase: 'backfilling',
      discovered_conversations: 4,
      complete_conversations: 2,
      complete_as_of: null,
      reason: 'conversation_evidence_missing',
    };
    const store = readyStore(
      snapshot({
        analyticsView: analytics([7, 0, null, 4], 'synced_subset'),
        coverage: partialCoverage,
      }),
    );

    renderDashboard(store);

    expectKpi('Total conversations', '7+');
    expectKpi('Total messages', '0 in synced messages');
    expectKpi('Inbound messages', '—');
    expectKpi('Outbound messages', '4+');
    expect(screen.getByText('Historical coverage 50%', { exact: true })).toBeTruthy();
    expect(screen.getAllByText(/Based on synced messages/)).toHaveLength(4);
    expect(screen.getByText(/Trends become available only after historical coverage/)).toBeTruthy();
  });

  it('renders complete metric envelopes without deriving charts from latest previews', () => {
    const store = readyStore();

    renderDashboard(store);

    expectKpi('Total conversations', '1');
    expectKpi('Total messages', '19');
    expectKpi('Inbound messages', '11');
    expectKpi('Outbound messages', '8');
    expect(screen.getByText('Up to date', { exact: true })).toBeTruthy();
    expect(screen.getAllByText(/Complete range/)).toHaveLength(4);
    expect(
      screen.getByText('Daily series are not included in the bounded Bridge snapshot.'),
    ).toBeTruthy();
    expect(
      screen.getByText(
        'Sentiment requires complete historical coverage and a projected sentiment series.',
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        'Conversation ranking requires complete per-conversation message aggregates.',
      ),
    ).toBeTruthy();
    expect(screen.queryByText('Bounded latest preview')).toBeNull();
  });

  it('applies the next analytics envelope and projection revision atomically', () => {
    const store = readyStore();
    renderDashboard(store);
    expectKpi('Total messages', '19');

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

    expectKpi('Total messages', '20');
    expectKpi('Outbound messages', '9');
    expect(screen.getByText('Up to date', { exact: true })).toBeTruthy();
  });

  it('renders metric values unavailable when the projection is unavailable', () => {
    const store = readyStore(
      snapshot({
        projection: {
          ...CURRENT_PROJECTION,
          status: 'unavailable',
          reason: 'projection_generation_failed',
        },
      }),
    );

    renderDashboard(store);

    expectKpi('Total conversations', '—');
    expectKpi('Total messages', '—');
    expect(screen.getByRole('alert').textContent).toContain('Analytics unavailable');
  });

  it('retains the latest evidence-backed values through resync and disconnect', () => {
    const store = readyStore();
    renderDashboard(store);

    act(() => store.beginResync());
    expect(screen.getByText('Resyncing', { exact: true })).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Refreshing analytics');
    expectKpi('Total messages', '19');

    act(() => store.markDisconnected());
    expect(screen.getByText('Cached', { exact: true })).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Realtime updates paused');
    expectKpi('Total messages', '19');
  });

  it('replaces a raw coverage reason code with friendly text when history sync is blocked', () => {
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

    renderDashboard(store);

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Historical sync was turned off');
    expect(alert.textContent).not.toContain('consent_revoked');
  });

  it('falls back to a generic message for an unrecognized coverage reason code', () => {
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

    renderDashboard(store);

    const alert = screen.getByRole('alert');
    expect(alert.textContent).not.toContain('agent_reported_a_new_code');
    expect(alert.textContent).toContain('This needs attention');
  });
});
