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

import { createBridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';
import CreatorDashboardView from '../src/views/CreatorDashboardView';
import { desktopFixture } from './desktopOnboardingFixture';

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
    browser: null,
  });
  store.applySnapshot(payload);
  return store;
}

function readinessSource(ready: boolean | null) {
  const fixture = desktopFixture(false);
  if (ready === null) fixture.publish({ snapshot: null });
  else fixture.readiness({ schema: 'ofca-analysis-readiness/v1',
    commercial_authority: ready ? 'active' : 'required', analysis_admission: ready ? 'admitted' : 'blocked' }, ACCOUNT_ID);
  return fixture;
}

function mountDashboard(store: ReturnType<typeof createBridgeTransportStore>, source: ReturnType<typeof desktopFixture>) {
  return render(<ThemeProvider theme={theme} defaultMode="light"><MemoryRouter>
    <CreatorDashboardView activationApi={{ redeem: vi.fn(async () => ({ state: 'checking' as const })) }}
      store={store} onboardingApi={source.api} />
  </MemoryRouter></ThemeProvider>);
}

async function renderDashboard(store: ReturnType<typeof createBridgeTransportStore>, source = readinessSource(true)) {
  const view = mountDashboard(store, source);
  await act(async () => undefined);
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
  window.location.hash = '';
});

describe('CreatorDashboardView', () => {
  it('uses the reference empty card for confirmed zero activity, then renders arriving data', async () => {
    window.location.hash = '#journey=11111111-1111-4111-8111-111111111111';
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0]) }), conversations: [] });
    await renderDashboard(store);
    expect(screen.getByRole('heading', { name: 'No new activity yet' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Overview' })).toBeNull();
    expect(screen.getByText('Not checked', { exact: true })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Review' }).getAttribute('href')).toBe('/settings#message-history');
    await act(async () => { store.applySnapshot(snapshot({ viewRevision: 2 })); });
    expect(screen.queryByRole('heading', { name: 'No new activity yet' })).toBeNull();
    expectStat('Messages', '19');
  });
  it('does not treat unavailable counts as an empty received snapshot', async () => {
    window.location.hash = '#journey=11111111-1111-4111-8111-111111111111';
    await renderDashboard(readyStore({ ...snapshot({ analyticsView: analytics([null, null, null, null]) }), conversations: [] }));
    expect(screen.queryByRole('heading', { name: 'No new activity yet' })).toBeNull();
    expect(screen.getByRole('region', { name: 'Overview' })).toBeTruthy();
  });
  it('shows a paused browser without a ready claim in the empty journey', async () => {
    window.location.hash = '#journey=11111111-1111-4111-8111-111111111111';
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0]) }), conversations: [] });
    store.setAgent({ ...store.getState().agent!, browser: { capture: 'paused', site_access: 'granted',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF } });
    await renderDashboard(store);
    expect(screen.getByText('Paused', { exact: true })).toBeTruthy();
    expect(screen.queryByText("You're all set")).toBeNull();
  });
  it('shows the saved chat count after history completes', async () => {
    await renderDashboard(readyStore());
    expect(overview().getByText('History saved for 1 chats')).toBeTruthy();
    expect(overview().queryByRole('progressbar')).toBeNull();
  });
  it('shows available metrics while setup readiness remains unknown', () => {
    const api = readinessSource(null);
    mountDashboard(readyStore(), api);
    expectStat('Messages', '19');
    expect(screen.getByText('Checking setup…')).toBeTruthy();
    expect(screen.queryByText('Finish setup')).toBeNull();
  });
  it('reserves metric skeletons before the first snapshot', async () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);

    await renderDashboard(store);

    expect(screen.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Finish setup' })).toBeNull();
    expect(screen.getByRole('region', { name: 'Overview' })).toBeTruthy();
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

  it('offers optional Full analytics alongside the accessible dashboard', async () => {
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

    await renderDashboard(store, readinessSource(false));

    expect(screen.queryByRole('region', { name: 'Finish setup' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Full analytics' })).toBeTruthy();
    expect(screen.queryByText('Turn on message history')).toBeNull();
    expect(screen.getByRole('region', { name: 'Overview' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Recent conversations' })).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('does not require setup when Full is ready and older-history import was skipped', async () => {
    const store = readyStore(snapshot({
      analyticsView: analytics([0, 0, 0, 0], 'synced_subset'),
      coverage: { ...COMPLETE_COVERAGE, status: 'partial', phase: 'not_started',
        discovered_conversations: null, complete_conversations: 0, complete_as_of: null },
    }));
    await renderDashboard(store, readinessSource(true));
    expect(screen.queryByRole('region', { name: 'Finish setup' })).toBeNull();
    expect(screen.getByRole('region', { name: 'Overview' })).toBeTruthy();
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

  it('withholds unavailable counts without claiming that blocked history is syncing', async () => {
    const store = readyStore(
      snapshot({
        coverage: {
          ...COMPLETE_COVERAGE,
          status: 'partial',
          phase: 'blocked',
          complete_as_of: null,
          reason: 'consent_revoked',
        },
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
    expect(alert.textContent).not.toMatch(/keep syncing|in the meantime|will appear/iu);
  });

  it('reports missing counts through resync and disconnect without a recovery promise', async () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.setConnection('connected');
    store.beginResync();
    await renderDashboard(store);

    expect(screen.getByRole('alert').textContent).toContain('Refreshing your numbers');
    expect(store.getState().viewRevision).toBeNull();
    expect(within(overview().getByRole('group', { name: 'Messages' })).queryByText('19')).toBeNull();
    expect(screen.getByRole('alert').textContent).not.toMatch(/will appear|in a moment/iu);

    act(() => store.markDisconnected());
    expect(screen.getByRole('alert').textContent).toContain('Dashboard connection interrupted');
    expect(store.getState().viewRevision).toBeNull();
    expect(screen.getByRole('alert').textContent).not.toMatch(/will appear|once the connection/iu);
  });

  it('keeps the last counts through resync and disconnect', async () => {
    const store = readyStore();
    await renderDashboard(store);
    expect(screen.queryByRole('alert')).toBeNull();

    act(() => store.beginResync());
    expect(screen.getByRole('alert').textContent).toContain('Refreshing your numbers');
    expectStat('Messages', '19');

    act(() => store.markDisconnected());
    expect(screen.queryByRole('alert')).toBeNull();
    expectStat('Messages', '19');

    act(() => store.setConnection('connected'));
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Updates unavailable');
    expect(alert.textContent).toContain('Showing your last numbers.');
    expect(alert.textContent).not.toMatch(/reconnecting|paused|until/iu);
    expectStat('Messages', '19');
  });

  it.each(['pending', 'degraded', 'unavailable', 'empty'] as const)(
    'does not claim last numbers are displayed when %s metrics render as unknown',
    async (caseName) => {
      const store = readyStore(snapshot({
        analyticsView: caseName === 'empty' ? analytics([null, null, null, null]) : undefined,
        projection: caseName === 'empty' ? CURRENT_PROJECTION : {
          ...CURRENT_PROJECTION, status: caseName, reason: 'projection_lag',
        },
      }));
      await renderDashboard(store);
      expectStat('Messages', '—');

      act(() => store.beginResync());
      expect(screen.getByRole('alert').textContent).toBe('Refreshing your numbers');
      expectStat('Messages', '—');
      act(() => {
        store.markDisconnected();
        store.setConnection('connected');
      });
      expect(screen.getByRole('alert').textContent).toBe('Updates unavailable');
      expectStat('Messages', '—');
    },
  );

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

  it('states the blocked history outcome without inventing a remedy for an unknown reason', async () => {
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
    expect(alert.textContent).toBe('History incomplete');
    expect(alert.textContent).not.toMatch(/needs attention|stopped|Settings|Resume|will/iu);
  });

  it('renders the setup prompt and overview together when the shared owner view arrives', async () => {
    const source = readinessSource(null);
    mountDashboard(readyStore(), source);
    expect(screen.getByText('Checking setup…')).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Overview' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Finish setup' })).toBeNull();
    const current = readinessSource(false).api.getState();
    await act(async () => source.publish(current));
    const prompt = screen.getByRole('heading', { name: 'Full analytics' });
    const overview = screen.getByRole('region', { name: 'Overview' });
    expect(overview.compareDocumentPosition(prompt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText('Loading dashboard…')).toBeNull();
    expectStat('Messages', '19');
  });

  it('uses the shared current readiness on remount while keeping saved metrics', async () => {
    const store = readyStore(), source = readinessSource(false);
    const first = await renderDashboard(store, source); first.unmount();
    mountDashboard(store, source);
    expect(screen.queryByText('Checking setup…')).toBeNull();
    expect(screen.getByRole('heading', { name: 'Full analytics' })).toBeTruthy();
    expectStat('Messages', '19');
    expect(source.api.begin).not.toHaveBeenCalled();
  });

  it('retains saved metrics through readiness interruption and owner recovery without replay', async () => {
    const source = readinessSource(true);
    mountDashboard(readyStore(), source);
    await act(async () => source.publish({ connected: false, error: 'service_unavailable' }));
    expectStat('Messages', '19');
    expect(screen.queryByText('Checking setup…')).toBeNull();
    await act(async () => source.publish({ connected: true, error: null }));
    expect(screen.queryByRole('heading', { name: 'Full analytics' })).toBeNull();
    expectStat('Messages', '19');
    expect(source.api.begin).not.toHaveBeenCalled();
  });

  it('cannot use delayed readiness for another creator', async () => {
    const source = readinessSource(true);
    source.readiness({ schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'admitted' }, 'another-creator');
    mountDashboard(readyStore(), source);
    expect(screen.getByRole('heading', { name: 'Full analytics' })).toBeTruthy();
    expectStat('Messages', '19');
    expect(screen.queryByText('On')).toBeNull();
    expect(source.api.begin).not.toHaveBeenCalled();
  });
});
