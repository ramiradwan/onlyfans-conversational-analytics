import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AnalyticsMetric,
  AnalyticsView,
  BrowserSurfacePayload,
  ConversationSummary,
  HistoricalCoverage,
  HistorySettings,
  ProjectionState,
  StateSnapshotPayload,
} from '../src/protocol';

import { createBridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';
import CreatorDashboardView from '../src/views/CreatorDashboardView';
import { desktopFixture } from './desktopOnboardingFixture';
import type { BrowserControlApi } from '../src/services/browserControlApi';
import type { HistorySettingsApi } from '../src/services/historySettingsApi';

const ACCOUNT_ID = 'creator-account';
const AS_OF = '2026-07-19T12:00:00Z';
const COLLECTION_ON: BrowserSurfacePayload = { capture: 'active', site_access: 'granted',
  history_permission: 'missing', legal_review_required: false, reported_at: AS_OF };
const SAVED_BASIS = 'Based on 19 messages saved on this computer so far.';
const LAST_UPDATED = `Last updated ${new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }).format(new Date(AS_OF))}.`;

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
  const directionalSampleSize = (values[2] ?? 0) + (values[3] ?? 0);
  return {
    total_conversations: metric(values[0], basis, values[0] ?? 0, projectionRevision),
    total_messages: metric(values[1], basis, values[1] ?? 0, projectionRevision),
    inbound_messages: metric(values[2], basis, directionalSampleSize, projectionRevision),
    outbound_messages: metric(values[3], basis, directionalSampleSize, projectionRevision),
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

function mountDashboard(store: ReturnType<typeof createBridgeTransportStore>, source: ReturnType<typeof desktopFixture>,
  browserApi?: BrowserControlApi, historyApi?: HistorySettingsApi) {
  return render(<ThemeProvider theme={theme} defaultMode="light"><MemoryRouter>
    <CreatorDashboardView activationApi={{ redeem: vi.fn(async () => ({ state: 'checking' as const })) }}
      store={store} onboardingApi={source.api} browserApi={browserApi} historyApi={historyApi} historyStore={store} />
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
  it('opens without actions until the account is bound, then shows count placeholders', () => {
    const store = createBridgeTransportStore();
    mountDashboard(store, readinessSource(null));
    expect(screen.getByRole('heading', { name: 'Opening Dipsy' })).toBeTruthy();
    expect(screen.getByText('Checking your connection.')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
    act(() => store.bindAccount(ACCOUNT_ID));
    expect(overview().getByRole('group', { name: 'Messages' }).querySelector('.MuiSkeleton-root')).toBeTruthy();
    expect(screen.queryByText('Checking setup…')).toBeNull();
  });

  it('shows the browser prerequisite instead of a zero-count claim', () => {
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0]) }), conversations: [] });
    store.setAgent({ ...store.getState().agent!, status: 'disconnected' });
    mountDashboard(store, readinessSource(true));
    expect(screen.getByRole('heading', { name: 'Set up Chrome to see your message counts' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Continue in Chrome' }).getAttribute('href')).toBe('/settings#browser-extension');
    expect(screen.queryByRole('heading', { name: 'No messages to show yet' })).toBeNull();
    expect(screen.queryByText('Results will appear when Dipsy receives messages.')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Overview' })).toBeNull();
  });

  it('uses the reference empty card for confirmed zero activity, then renders arriving data', async () => {
    window.location.hash = '#journey=11111111-1111-4111-8111-111111111111';
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0]) }), conversations: [] });
    store.setAgent({ ...store.getState().agent!, browser: COLLECTION_ON });
    await renderDashboard(store);
    expect(screen.getByRole('heading', { name: 'No messages to show yet' })).toBeTruthy();
    expect(screen.getByText('Results will appear when Dipsy receives messages.')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Overview' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review message history' })).toBeNull();
    await act(async () => { store.applySnapshot(snapshot({ viewRevision: 2 })); });
    expect(screen.queryByRole('heading', { name: 'No messages to show yet' })).toBeNull();
    expectStat('Messages', '19');
  });

  it('waits for messages while collection is on and the empty sample is incomplete', async () => {
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0], 'synced_subset'),
      coverage: { ...COMPLETE_COVERAGE, status: 'partial', phase: 'not_started', complete_as_of: null } }), conversations: [] });
    store.setAgent({ ...store.getState().agent!, browser: COLLECTION_ON });
    useUserStore.getState().actions.setUserRole(null);
    await renderDashboard(store);
    expect(screen.getByRole('heading', { name: 'Waiting for messages' })).toBeTruthy();
    expect(screen.getByText('Results will appear when Dipsy receives messages.')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'No messages to show yet' })).toBeNull();
  });

  it('does not use complete empty counts when live freshness is delayed', async () => {
    const payload = snapshot({ analyticsView: analytics([0, 0, 0, 0]) });
    const store = readyStore({ ...payload, conversations: [], live_freshness: { ...payload.live_freshness, status: 'delayed' } });
    store.setAgent({ ...store.getState().agent!, browser: COLLECTION_ON });
    await renderDashboard(store);
    expect(screen.queryByRole('heading', { name: 'No messages to show yet' })).toBeNull();
    expect(overview().getByRole('group', { name: 'Messages' }).textContent).not.toContain('0');
  });
  it('does not treat unavailable counts as an empty received snapshot', async () => {
    window.location.hash = '#journey=11111111-1111-4111-8111-111111111111';
    await renderDashboard(readyStore({ ...snapshot({ analyticsView: analytics([null, null, null, null]) }), conversations: [] }));
    expect(screen.queryByRole('heading', { name: 'No messages to show yet' })).toBeNull();
    expect(screen.getByRole('region', { name: 'Overview' })).toBeTruthy();
  });
  it('shows a paused browser without a ready claim in the empty journey', async () => {
    window.location.hash = '#journey=11111111-1111-4111-8111-111111111111';
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0]) }), conversations: [] });
    store.setAgent({ ...store.getState().agent!, browser: { capture: 'paused', site_access: 'granted',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF } });
    await renderDashboard(store);
    expect(screen.getByRole('heading', { name: 'Paused by you' })).toBeTruthy();
    expect(screen.getByText('New messages aren’t being collected.')).toBeTruthy();
    expect(screen.queryByText('Results will appear when Dipsy receives messages.')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review message history' })).toBeNull();
    expect(screen.queryByText("You're all set")).toBeNull();
  });

  it('opens collection controls once only for an explicit user pause', async () => {
    const store = readyStore();
    const api: BrowserControlApi = { setCapture: vi.fn(), open: vi.fn(async () => 'delivered') };
    store.setAgent({ ...store.getState().agent!, browser: COLLECTION_ON });
    mountDashboard(store, readinessSource(true), api);
    expect(screen.queryByRole('button', { name: 'Review in Chrome' })).toBeNull();
    act(() => store.setAgent({ ...store.getState().agent!, browser: { ...COLLECTION_ON, capture: 'paused' } }));
    const review = screen.getByRole('button', { name: 'Review in Chrome' });
    expect(api.open).not.toHaveBeenCalled();
    await act(async () => { fireEvent.click(review); fireEvent.click(review); });
    expect(api.open).toHaveBeenCalledExactlyOnceWith('setup', expect.any(AbortSignal));
    expect(api.setCapture).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Paused by you' })).toBeTruthy();
  });

  it('reports a browser launch failure only after an undelivered review action', async () => {
    const store = readyStore();
    store.setAgent({ ...store.getState().agent!, browser: { ...COLLECTION_ON, capture: 'paused' } });
    const api: BrowserControlApi = { setCapture: vi.fn(), open: vi.fn(async () => 'unreachable') };
    mountDashboard(store, readinessSource(true), api);
    expect(screen.queryByText('Chrome didn’t open. Open Chrome to continue.')).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Review in Chrome' })));
    expect(screen.getByText('Chrome didn’t open. Open Chrome to continue.')).toBeTruthy();
  });

  it('places the live saved basis after both directional counts and omits Last updated', async () => {
    await renderDashboard(readyStore());
    const basis = screen.getByText((text) => text.startsWith(SAVED_BASIS));
    const sent = overview().getByText('Sent', { exact: true }).parentElement!;
    expect(sent.compareDocumentPosition(basis) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText(/Last updated/)).toBeNull();
    expect(basis.textContent).toBe(SAVED_BASIS);
  });

  it.each(['revision', 'time', 'range', 'basis', 'sample size', 'missing'] as const)('suppresses the saved basis when directional evidence has a mismatched %s', async (mismatch) => {
    const values = analytics([1, 19, 11, 8]);
    const sent = values.outbound_messages;
    if (mismatch === 'revision') sent.projection_revision = 3;
    if (mismatch === 'time') sent.as_of = '2026-07-19T11:59:00Z';
    if (mismatch === 'range') sent.observed_range = { ...sent.observed_range, start: null };
    if (mismatch === 'basis') sent.basis = 'synced_subset';
    if (mismatch === 'sample size') sent.sample_size += 1;
    if (mismatch === 'missing') sent.value = null;
    await renderDashboard(readyStore(snapshot({ analyticsView: values })));
    expect(screen.queryByText(/Based on .* messages saved on this computer so far/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Details' })).toBeTruthy();
  });

  it('adds the real sample time when user pause stops updates', async () => {
    const store = readyStore();
    store.setAgent({ ...store.getState().agent!, browser: { ...COLLECTION_ON, capture: 'paused' } });
    await renderDashboard(store);
    expect(screen.getByText(`${SAVED_BASIS} ${LAST_UPDATED}`, { exact: true })).toBeTruthy();
  });

  it('omits an invalid timestamp even when updates have stopped', async () => {
    const values = analytics([1, 19, 11, 8]);
    values.inbound_messages.as_of = '';
    values.outbound_messages.as_of = '';
    const store = readyStore(snapshot({ analyticsView: values }));
    store.setAgent({ ...store.getState().agent!, browser: { ...COLLECTION_ON, capture: 'paused' } });
    await renderDashboard(store);
    expect(screen.getByText(SAVED_BASIS, { exact: true })).toBeTruthy();
    expect(screen.queryByText(/Last updated/)).toBeNull();
  });

  it('keeps the saved counts through a 3 second reconnect grace and clears the notice on recovery', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    try {
      const store = readyStore();
      mountDashboard(store, readinessSource(true));
      act(() => { store.markDisconnected(); store.setConnection('reconnecting'); });
      act(() => vi.advanceTimersByTime(2999));
      expect(screen.queryByText('Reconnecting to the app…')).toBeNull();
      expectStat('Messages', '19');
      act(() => vi.advanceTimersByTime(1));
      expect(screen.getByText('Reconnecting to the app…')).toBeTruthy();
      expect(screen.queryByText(/Last updated/)).toBeNull();
      act(() => store.setConnection('disconnected'));
      expect(screen.getByText('Dipsy can’t connect right now. Showing your saved counts.')).toBeTruthy();
      expect(screen.getByText(`${SAVED_BASIS} ${LAST_UPDATED}`, { exact: true })).toBeTruthy();
      act(() => { store.setConnection('connected'); store.applySnapshot(snapshot({ viewRevision: 2 })); });
      expect(screen.queryByText('Reconnecting to the app…')).toBeNull();
      expect(screen.queryByText('Dipsy can’t connect right now. Showing your saved counts.')).toBeNull();
      expectStat('Messages', '19');
    } finally { vi.useRealTimers(); }
  });

  it('describes an unverified update and the saved sample time after recovery stops', async () => {
    const store = readyStore();
    await renderDashboard(store);
    act(() => { store.markDisconnected(); store.setConnection('connected'); });
    expect(screen.getByText('Couldn’t check the latest update. Showing your saved counts.')).toBeTruthy();
    expect(screen.getByText(`${SAVED_BASIS} ${LAST_UPDATED}`, { exact: true })).toBeTruthy();
  });

  it('shows the update-required copy only for the classified software update', async () => {
    const store = readyStore();
    store.setAgent({ ...store.getState().agent!, browser: { ...COLLECTION_ON, history_error_code: 'unsupported_revision' } });
    await renderDashboard(store);
    expect(screen.queryByText('Update Conversation Analytics to continue.')).toBeNull();
    expect(screen.queryByText('Updating this page…')).toBeNull();
    act(() => store.setAgent({ ...store.getState().agent!, browser: { ...COLLECTION_ON, history_error_code: 'software_update_required' } }));
    expect(screen.getByText('Update Conversation Analytics to continue.')).toBeTruthy();
    expect(screen.queryByText(/Last updated/)).toBeNull();
  });
  it('opens the current browser review directly and never accepts collection from the dashboard', async () => {
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0]) }), conversations: [] });
    const browser: BrowserSurfacePayload = { capture: 'active', site_access: 'granted',
      history_permission: 'missing', legal_review_required: true, reported_at: AS_OF };
    store.setAgent({ ...store.getState().agent!, browser });
    const source = readinessSource(true);
    source.publish({ snapshot: { ...source.api.getState().snapshot!, allowed_actions: ['browser.access'] } });
    const api: BrowserControlApi = { setCapture: vi.fn(async () => 'delivered'), open: vi.fn(async () => 'delivered') };
    mountDashboard(store, source, api);
    expect(screen.getByRole('heading', { name: 'Allow message collection' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Review message history' })).toBeNull();
    expect(source.api.begin).not.toHaveBeenCalled();
    const review = screen.getByRole('button', { name: 'Review in Chrome' });
    await act(async () => { fireEvent.click(review); fireEvent.click(review); });
    expect(source.api.begin).toHaveBeenCalledExactlyOnceWith('browser.access');
    expect(api.setCapture).not.toHaveBeenCalled();
    expect(api.open).not.toHaveBeenCalled();
    await act(async () => source.publish({ snapshot: { ...source.api.getState().snapshot!, allowed_actions: [],
      pending_operations: [{ operation_id: '22222222-2222-4222-8222-222222222222', action: 'browser.access', state: 'waiting', reason: 'none' }] } }));
    expect(screen.queryByRole('button', { name: 'Review in Chrome' })).toBeNull();
    expect(screen.getByText('Complete the review in Chrome.')).toBeTruthy();
    await act(async () => store.setAgent({ ...store.getState().agent!, browser: { ...browser, legal_review_required: false } }));
    expect(screen.queryByRole('heading', { name: 'Allow message collection' })).toBeNull();
    // Complete history is not offered again, even with no visible messages.
    expect(screen.queryByRole('button', { name: 'Review message history' })).toBeNull();
    expect(source.api.begin).toHaveBeenCalledOnce();
  });

  it('does not offer browser review without matching current authority or hide existing activity', async () => {
    const store = readyStore();
    store.setAgent({ ...store.getState().agent!, browser: { capture: 'active', site_access: 'needs_approval',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF } });
    const source = readinessSource(true);
    mountDashboard(store, source);
    expectStat('Messages', '19');
    expect(screen.getByRole('heading', { name: 'Allow OnlyFans access' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Review in Chrome' })).toBeNull();
    await act(async () => source.publish({ snapshot: { ...source.api.getState().snapshot!, allowed_actions: ['browser.access'] } }));
    expect(screen.getByRole('button', { name: 'Review in Chrome' })).toBeTruthy();
    await act(async () => source.readiness({ schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active',
      analysis_admission: 'admitted' }, 'different-creator'));
    expect(screen.queryByRole('button', { name: 'Review in Chrome' })).toBeNull();
    expectStat('Messages', '19');
    expect(source.api.begin).not.toHaveBeenCalled();
  });

  it('leaves paused collection untouched after opening Chrome and waits for pushed confirmation', async () => {
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0]) }), conversations: [] });
    const browser: BrowserSurfacePayload = { capture: 'paused', site_access: 'granted',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF };
    store.setAgent({ ...store.getState().agent!, browser });
    const api: BrowserControlApi = { setCapture: vi.fn(async () => 'delivered'), open: vi.fn(async () => 'delivered') };
    const source = readinessSource(true);
    mountDashboard(store, source, api);
    expect(api.setCapture).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Review in Chrome' })));
    expect(api.open).toHaveBeenCalledExactlyOnceWith('setup', expect.any(AbortSignal));
    expect(api.setCapture).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Paused by you' })).toBeTruthy();
    await act(async () => store.setAgent({ ...store.getState().agent!, browser: { ...browser, capture: 'active' } }));
    expect(screen.queryByRole('heading', { name: 'Paused by you' })).toBeNull();
    expect(source.api.begin).not.toHaveBeenCalled();
  });

  it('offers admitted browser review before commercial activation without treating collection as activation', async () => {
    const store = readyStore();
    const browser: BrowserSurfacePayload = { capture: 'off', site_access: 'needs_approval',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF };
    store.setAgent({ ...store.getState().agent!, browser });
    const source = readinessSource(false);
    source.publish({ snapshot: { ...source.api.getState().snapshot!, allowed_actions: ['browser.access', 'activation.review'] } });
    const api: BrowserControlApi = { setCapture: vi.fn(async () => 'delivered'), open: vi.fn(async () => 'delivered') };
    mountDashboard(store, source, api);
    expect(screen.getByRole('heading', { name: 'Allow OnlyFans access' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Full analytics' }).getAttribute('href')).toBe('/settings#full-analytics');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(source.api.begin).not.toHaveBeenCalled();
    expectStat('Messages', '19');
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Review in Chrome' })));
    expect(source.api.begin).toHaveBeenCalledExactlyOnceWith('browser.access');
    expect(api.setCapture).not.toHaveBeenCalled();
    expect(api.open).not.toHaveBeenCalled();
    const operationId = '22222222-2222-4222-8222-222222222222';
    await act(async () => source.publish({ pending: operationId, pendingAction: 'browser.access', error: 'operation_unconfirmed' }));
    expect(screen.getByRole('heading', { name: 'Allow OnlyFans access' })).toBeTruthy();
    expect(screen.getByText('The browser review has not been confirmed.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Review in Chrome' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Full analytics' })).toBeNull();
    await act(async () => source.publish({ pending: null, pendingAction: undefined, error: null }));
    await act(async () => store.setAgent({ ...store.getState().agent!, browser: { ...browser, capture: 'active', site_access: 'granted' } }));
    expect(screen.queryByRole('heading', { name: 'Allow OnlyFans access' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Full analytics' })).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(source.api.begin).toHaveBeenCalledOnce();
  });

  it('does not force browser review before activation if the owner has not admitted it', () => {
    const store = readyStore();
    store.setAgent({ ...store.getState().agent!, browser: { capture: 'off', site_access: 'needs_approval',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF } });
    const source = readinessSource(false);
    source.publish({ snapshot: { ...source.api.getState().snapshot!, allowed_actions: ['activation.review'] } });
    mountDashboard(store, source);
    expect(screen.queryByRole('button', { name: 'Review in Chrome' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Set up Full analytics' })).toBeTruthy();
    expect(source.api.begin).not.toHaveBeenCalled();
  });

  it('can review previously authorized collection before commercial activation', async () => {
    const store = readyStore();
    store.setAgent({ ...store.getState().agent!, browser: { capture: 'paused', site_access: 'granted',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF } });
    const source = readinessSource(false);
    const api: BrowserControlApi = { setCapture: vi.fn(async () => 'delivered'), open: vi.fn(async () => 'delivered') };
    mountDashboard(store, source, api);
    expect(screen.getByRole('heading', { name: 'Paused by you' })).toBeTruthy();
    expect(api.setCapture).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Review in Chrome' })));
    expect(api.open).toHaveBeenCalledExactlyOnceWith('setup', expect.any(AbortSignal));
    expect(api.setCapture).not.toHaveBeenCalled();
    expect(source.api.begin).not.toHaveBeenCalled();
  });

  it('retains the admitted commercial operation instead of opening a competing capture review', async () => {
    const store = readyStore();
    store.setAgent({ ...store.getState().agent!, browser: { capture: 'off', site_access: 'needs_approval',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF } });
    const source = readinessSource(false);
    source.publish({ snapshot: { ...source.api.getState().snapshot!, allowed_actions: ['browser.access'],
      pending_operations: [{ operation_id: '22222222-2222-4222-8222-222222222222',
        action: 'activation.review', state: 'outcome_unknown', reason: 'operation_unconfirmed' }] } });
    mountDashboard(store, source);
    expect(screen.queryByRole('button', { name: 'Review in Chrome' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Full analytics' })).toBeTruthy();
    expect(screen.getByText('Activation has not been confirmed.')).toBeTruthy();
    expect(source.api.begin).not.toHaveBeenCalled();
    await act(async () => source.publish({ snapshot: { ...source.api.getState().snapshot!, pending_operations: [],
      allowed_actions: ['browser.access'] } }));
    expect(screen.getByRole('button', { name: 'Review in Chrome' })).toBeTruthy();
    expect(source.api.begin).not.toHaveBeenCalled();
  });

  it('keeps a currently admissible commercial review available independently of the extension connection', () => {
    const store = readyStore();
    store.setAgent({ ...store.getState().agent!, status: 'disconnected' });
    const source = readinessSource(false);
    source.publish({ snapshot: { ...source.api.getState().snapshot!, allowed_actions: ['activation.review'] } });
    mountDashboard(store, source);
    expect(screen.getByRole('button', { name: 'Set up Full analytics' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Set up Chrome to see your message counts' })).toBeNull();
    expect(source.api.begin).not.toHaveBeenCalled();
  });

  function historyReviewFixture(creator = ACCOUNT_ID, saved: Partial<HistorySettings> = {}, permission: BrowserSurfacePayload['history_permission'] = 'missing', phase: HistoricalCoverage['phase'] = 'not_started') {
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0]),
      coverage: { ...COMPLETE_COVERAGE, status: 'partial', phase, complete_as_of: null } }), conversations: [] });
    const browser: BrowserSurfacePayload = { capture: 'active', site_access: 'granted',
      history_permission: permission, legal_review_required: false, reported_at: AS_OF };
    store.setAgent({ ...store.getState().agent!, browser, required_history_settings_revision: 1, applied_history_settings_revision: 1 });
    const settings: HistorySettings = { creator_account_id: creator, settings_revision: 1,
      consent_policy_version: 'history-consent-v1', consent_revision: null, authorized_platform_creator_id: null,
      desired_state: 'not_started', effective_state: 'not_applied', effective_config_revision: null,
      recent_window_days: 30, page_size: 50, pages_per_wake: 2, request_interval_ms: 1000, retry_limit: 3, updated_at: AS_OF, ...saved };
    const running: HistorySettings = { ...settings, settings_revision: 2, consent_revision: 'consent-1',
      authorized_platform_creator_id: 'platform-creator-1', desired_state: 'running', effective_state: 'running' };
    const api = { get: vi.fn(async () => settings), update: vi.fn(async () => running), revoke: vi.fn() };
    const browserApi = { open: vi.fn(async () => 'delivered' as const), setCapture: vi.fn() };
    const source = readinessSource(true);
    mountDashboard(store, source, browserApi, api);
    return { store, browser, api, browserApi, source, settings };
  }

  it('reviews history in the dashboard, then requests Chrome permission only after explicit consent', async () => {
    const { store, browser, api, browserApi } = historyReviewFixture();
    fireEvent.click(await screen.findByRole('button', { name: 'Review message history' }));
    expect(api.get).toHaveBeenCalledOnce();
    const dialog = screen.getByRole('dialog', { name: 'Add older conversations' });
    const disclosure = [
      'Read-only: it never sends messages or changes your account.',
      'Synced message history stays on this computer.',
      'Pause or turn it off at any time.',
    ];
    const readOnly = within(dialog).getByText(disclosure[0], { exact: true });
    expect([...readOnly.parentElement!.children].map(item => item.textContent)).toEqual(disclosure);
    const consent = within(dialog).getByRole('checkbox', {
      name: 'I allow read-only syncing of my older messages to this computer.',
    });
    expect(readOnly.parentElement!.compareDocumentPosition(consent) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const allow = await within(dialog).findByRole('button', { name: 'Allow message history' });
    expect(allow.parentElement).toBe(within(dialog).getByRole('button', { name: 'Not now' }).parentElement);
    expect(allow.hasAttribute('disabled')).toBe(true);
    expect(api.update).not.toHaveBeenCalled();
    expect(consent.compareDocumentPosition(allow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(consent);
    await act(async () => { fireEvent.click(allow); fireEvent.click(allow); });
    await waitFor(() => expect(api.update).toHaveBeenCalledExactlyOnceWith(1, expect.objectContaining({
      desired_state: 'running', accept_consent: true, consent_policy_version: 'history-consent-v1',
      recent_window_days: 30, page_size: 50,
    }), expect.any(AbortSignal)));
    expect(browserApi.open).not.toHaveBeenCalled();
    const allowChrome = await within(dialog).findByRole('button', { name: 'Allow Chrome access' });
    expect(allowChrome.parentElement).toBe(within(dialog).getByRole('button', { name: 'Close' }).parentElement);
    fireEvent.click(allowChrome);
    await waitFor(() => expect(browserApi.open).toHaveBeenCalledExactlyOnceWith('history', expect.any(AbortSignal)));
    await act(async () => store.setAgent({ ...store.getState().agent!, browser: { ...browser, history_permission: 'granted' } }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add older conversations' })).toBeNull());
    expect(screen.getByRole('heading', { name: 'Starting message history…' })).toBeTruthy();
    expect(api.update).toHaveBeenCalledOnce();
  });

  it('keeps the dashboard permission decision open after a denied grant without reopening Chrome', async () => {
    const { store, browser, api, browserApi } = historyReviewFixture();
    const review = await screen.findByRole('button', { name: 'Review message history' });
    expect(screen.getByRole('heading', { name: 'Add older conversations' })).toBeTruthy();
    expect(screen.getByText('Use older messages to see results sooner.')).toBeTruthy();
    review.focus();
    fireEvent.click(review);
    const dialog = screen.getByRole('dialog', { name: 'Add older conversations' });
    expect(within(dialog).getByText('Use older messages to see results sooner. Chrome may ask you to allow access next.')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('checkbox'));
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Allow message history' })));
    const allow = within(dialog).getByRole('button', { name: 'Allow Chrome access' });
    await act(async () => fireEvent.click(allow));
    await act(async () => store.setAgent({ ...store.getState().agent!, browser: { ...browser, history_permission: 'missing' } }));
    expect(within(dialog).getByText('Older messages start syncing after Chrome allows access.')).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: 'Allow Chrome access' })).toBe(allow);
    expect(browserApi.open).toHaveBeenCalledExactlyOnceWith('history', expect.any(AbortSignal));
    expect(api.update).toHaveBeenCalledOnce();
    expect(screen.queryByText('Adding older messages. Message counts update as they arrive.')).toBeNull();
  });

  it('does not offer history for a different creator', async () => {
    const { api, browserApi } = historyReviewFixture('other-creator');
    await screen.findByText('Message history settings are unavailable for this account.');
    expect(screen.queryByRole('button', { name: 'Review message history' })).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(api.update).not.toHaveBeenCalled();
    expect(browserApi.open).not.toHaveBeenCalled();
  });

  it('keeps a dismissed optional history offer quiet across session renewal', async () => {
    const { api, source } = historyReviewFixture();
    fireEvent.click(await screen.findByRole('button', { name: 'Not now' }));
    expect(screen.getByRole('heading', { name: 'Waiting for messages' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Message history' })).toBeTruthy();
    await act(async () => source.publish({ snapshot: { ...source.api.getState().snapshot!,
      generation: '33333333-3333-4333-8333-333333333333' } }));
    expect(screen.queryByRole('button', { name: 'Not now' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Message history' })).toBeTruthy();
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.update).not.toHaveBeenCalled();
  });

  it('resumes paused history using the existing consent without manufacturing a new receipt', async () => {
    const { api, browserApi } = historyReviewFixture(ACCOUNT_ID, {
      consent_revision: 'consent-1', authorized_platform_creator_id: 'platform-creator-1',
      desired_state: 'paused', effective_state: 'paused',
    }, 'granted');
    expect(await screen.findByRole('heading', { name: 'Message history is paused' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Review message history' }));
    const dialog = screen.getByRole('dialog', { name: 'Add older conversations' });
    expect(within(dialog).queryByRole('checkbox')).toBeNull();
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Resume' })));
    expect(api.update).toHaveBeenCalledExactlyOnceWith(1, expect.objectContaining({
      desired_state: 'running', accept_consent: false, consent_policy_version: null,
    }), expect.any(AbortSignal));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add older conversations' })).toBeNull());
    expect(screen.getByRole('heading', { name: 'Starting message history…' })).toBeTruthy();
    expect(browserApi.open).not.toHaveBeenCalled();
  });

  it('shows existing authorized history as starting or adding without offering consent again', async () => {
    const { api } = historyReviewFixture(ACCOUNT_ID, {
      consent_revision: 'consent-1', authorized_platform_creator_id: 'platform-creator-1',
      desired_state: 'running', effective_state: 'not_applied',
    }, 'granted');
    expect(await screen.findByRole('heading', { name: 'Starting message history…' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Review message history' })).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(api.update).not.toHaveBeenCalled();
  });

  it('keeps the history continuation visible during acquisition and does not offer consent again', async () => {
    const { api } = historyReviewFixture(ACCOUNT_ID, {
      consent_revision: 'consent-1', authorized_platform_creator_id: 'platform-creator-1',
      desired_state: 'running', effective_state: 'running',
    }, 'granted', 'backfilling');
    expect(await screen.findByText('Adding older messages. Message counts update as they arrive.')).toBeTruthy();
    expect(within(screen.getByRole('region', { name: 'Message history' })).queryByRole('progressbar')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review message history' })).toBeNull();
    expect(api.update).not.toHaveBeenCalled();
  });

  it('offers an explicit resume when coverage is already paused', async () => {
    const { api } = historyReviewFixture(ACCOUNT_ID, {
      consent_revision: 'consent-1', authorized_platform_creator_id: 'platform-creator-1',
      desired_state: 'paused', effective_state: 'paused',
    }, 'granted', 'paused');
    expect(await screen.findByRole('heading', { name: 'Message history is paused' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Review message history' })).toBeTruthy();
    expect(api.update).not.toHaveBeenCalled();
  });

  it('shows the first saved message without waiting for history or asking for another action', async () => {
    const coverage: HistoricalCoverage = { ...COMPLETE_COVERAGE, status: 'unknown', phase: 'not_started',
      generation_id: null, as_of: null, discovered_conversations: null, complete_conversations: 0, complete_as_of: null };
    const store = readyStore({ ...snapshot({ analyticsView: analytics([0, 0, 0, 0], 'synced_subset'), coverage }), conversations: [] });
    store.setAgent({ ...store.getState().agent!, browser: { capture: 'active', site_access: 'granted',
      history_permission: 'missing', legal_review_required: false, reported_at: AS_OF } });
    const source = readinessSource(true);
    const historyApi = { get: vi.fn(() => new Promise<HistorySettings>(() => {})), update: vi.fn(), revoke: vi.fn() };
    mountDashboard(store, source, undefined, historyApi);
    expect(screen.getByRole('heading', { name: 'Waiting for messages' })).toBeTruthy();
    expect(historyApi.get).toHaveBeenCalledOnce();
    expect(historyApi.update).not.toHaveBeenCalled();
    await act(async () => {
      store.applySnapshot(snapshot({ analyticsView: analytics([1, 1, 1, 0], 'synced_subset', 5),
        coverage, projection: { ...CURRENT_PROJECTION, canonical_revision: 5, projected_revision: 5 }, viewRevision: 2 }));
    });
    expect(screen.queryByRole('heading', { name: 'Waiting for messages' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review message history' })).toBeNull();
    expectStat('Messages', '1+');
    expectSplit('Received', '1+');
    expect(screen.getByText('Based on 1 messages saved on this computer so far.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open inbox' }).getAttribute('href')).toBe('/inbox');
    expect(source.api.begin).not.toHaveBeenCalled();
    expect(historyApi.update).not.toHaveBeenCalled();
  });

  it('does not show an unverified metric as first activity', async () => {
    const store = readyStore({ ...snapshot({ analyticsView: analytics([1, 1, 1, 0], 'synced_subset', 3) }) });
    await renderDashboard(store);
    expectStat('Messages', '\u2014');
    expect(screen.queryByText(/Based on .* messages saved on this computer so far/)).toBeNull();
  });

  it('does not show a percentage before discovery closes or before history is verified complete', async () => {
    const coverage: HistoricalCoverage = { ...COMPLETE_COVERAGE, status: 'partial', phase: 'discovering',
      discovered_conversations: 4, complete_conversations: 0, complete_as_of: null };
    const store = readyStore(snapshot({ analyticsView: analytics([1, 1, 1, 0], 'synced_subset'), coverage }));
    await renderDashboard(store);
    expect(overview().getByRole('progressbar', { name: 'Finding conversations' }).getAttribute('aria-valuenow')).toBeNull();
    expect(store.getState().snapshotProgress.percentage).toBeNull();
    await act(async () => store.applySnapshot(snapshot({ analyticsView: analytics([1, 1, 1, 0], 'synced_subset'),
      coverage: { ...coverage, phase: 'backfilling', complete_conversations: 4 }, viewRevision: 2 })));
    expect(overview().getByRole('progressbar', { name: 'Checking saved history' }).getAttribute('aria-valuenow')).toBeNull();
    expect(store.getState().snapshotProgress.percentage).toBeNull();
    expect(screen.queryByText('History 100% synced')).toBeNull();
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
    expect(screen.queryByText('Checking setup…')).toBeNull();
    expect(screen.queryByText('Set up Chrome to see your message counts')).toBeNull();
  });
  it('reserves metric skeletons before the first snapshot', async () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);

    await renderDashboard(store);

    expect(screen.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Set up Chrome to see your message counts' })).toBeNull();
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
    expect(screen.queryByText(/Based on .* messages saved on this computer so far/)).toBeNull();
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
    expect(screen.getByText('Based on 12 messages saved on this computer so far.')).toBeTruthy();
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
    expect(screen.getByText(SAVED_BASIS)).toBeTruthy();

    const details = screen.getByRole('button', { name: 'Details' });
    expect(details.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Messages counted', { exact: true })).toBeNull();
    fireEvent.click(details);
    expect(details.getAttribute('aria-expanded')).toBe('true');
    const popover = within(screen.getByRole('dialog', { name: 'How these numbers are counted' }));
    const counted = popover.getByText('Messages counted', { exact: true }).nextElementSibling;
    expect(counted?.textContent).toBe('19');
    expect(popover.getByText('Your full message history')).toBeTruthy();
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

    expect(screen.queryByRole('region', { name: 'Set up Chrome to see your message counts' })).toBeNull();
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
    expect(screen.queryByRole('region', { name: 'Set up Chrome to see your message counts' })).toBeNull();
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
    expect(alert.textContent).toBe('Couldn’t check the latest update. Showing your saved counts.');
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

  it('keeps the mounted overview ahead of a newly available readiness action', async () => {
    const source = readinessSource(null);
    mountDashboard(readyStore(), source);
    expect(screen.queryByText('Checking setup…')).toBeNull();
    const overview = screen.getByRole('region', { name: 'Overview' });
    expect(screen.queryByRole('region', { name: 'Set up Chrome to see your message counts' })).toBeNull();
    const current = readinessSource(false).api.getState();
    await act(async () => source.publish(current));
    const prompt = screen.getByRole('heading', { name: 'Full analytics' });
    expect(screen.getByRole('region', { name: 'Overview' })).toBe(overview);
    expect(overview.compareDocumentPosition(prompt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(prompt.closest('[data-reserved-region="dashboard-setup"]')).toBeTruthy();
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
