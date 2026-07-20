/** STORY ONLY: production presentations composed with deterministic fixtures. */
import { styled } from '@mui/material';
import { useEffect, useMemo } from 'react';

import {
  storyDateRange,
  storyWindowSources,
} from './analyticsFixtures';
import type { AnalyticsReadState } from '../analytics';
import {
  AnalyticsPresentation,
  CreatorDashboardPresentation,
} from '../components/analytics';
import type { StateSnapshotPayload } from '../protocol';
import { setAnalyticsStoryState } from '../store/analyticsStore';
import { createBridgeTransportStore } from '../store/transportStore';
import { GraphExplorerPresentation } from '../views/GraphExplorerView';
import OperatorInboxView from '../views/OperatorInboxView';

export type StoryViewName = 'dashboard' | 'analytics' | 'inbox' | 'graph';

const InboxStage = styled('div')({
  display: 'flex',
  flex: '1 1 auto',
  minHeight: 0,
});

function storyMetric(value: number, asOf: string) {
  return {
    value,
    basis: 'complete' as const,
    observed_range: { start: null, end: asOf },
    complete_range: { start: null, end: asOf },
    sample_size: value,
    as_of: asOf,
    projection_revision: 42,
  };
}

export function createStoryInboxStore() {
  const asOf = '2026-06-30T12:00:00.000Z';
  const store = createBridgeTransportStore();
  store.bindAccount('story-only-account');
  const snapshot: StateSnapshotPayload = {
    creator_account_id: 'story-only-account',
    view_revision: 42,
    generated_at: asOf,
    conversations: [
      {
        conversation_id: 'story-conversation-one',
        platform_user_id: 'story-participant-one',
        display_name: 'Story Participant',
        unread_count: 2,
        last_message_at: '2026-06-28T09:30:00.000Z',
        latest_message: {
          message_id: 'story-message-two',
          text: 'Synthetic response showing the conversation bubble width.',
          sent_at: '2026-06-28T09:30:00.000Z',
          direction: 'outbound',
          sentiment: 'positive',
        },
        coverage: {
          status: 'complete',
          boundary: 'history_start',
          earliest_available_at: '2026-06-01T00:00:00.000Z',
          latest_acquired_at: asOf,
          data_as_of: asOf,
          reason_code: null,
        },
      },
    ],
    analytics: {
      total_conversations: storyMetric(1, asOf),
      total_messages: storyMetric(2, asOf),
      inbound_messages: storyMetric(1, asOf),
      outbound_messages: storyMetric(1, asOf),
    },
    coverage: {
      status: 'complete',
      phase: 'complete',
      generation_id: '90000000-0000-4000-8000-000000000001',
      as_of: asOf,
      discovered_conversations: 1,
      complete_conversations: 1,
      complete_as_of: asOf,
      reason: null,
    },
    projection: {
      status: 'current',
      canonical_revision: 42,
      projected_revision: 42,
      projected_at: asOf,
      reason: null,
    },
    live_freshness: {
      status: 'current',
      last_observed_at: asOf,
      last_committed_at: asOf,
      expires_at: '2026-06-30T12:02:00.000Z',
      pending_count: 0,
      reason: null,
    },
  };
  store.applySnapshot(snapshot);
  return store;
}

export function StoryDashboardView({ state }: { state: AnalyticsReadState }) {
  return (
    <CreatorDashboardPresentation
      state={state}
      dateRange={storyDateRange}
      onDateRangeChange={() => undefined}
      windowSources={storyWindowSources}
    />
  );
}

export function StoryAnalyticsView({ state }: { state: AnalyticsReadState }) {
  return (
    <AnalyticsPresentation
      state={state}
      dateRange={storyDateRange}
      onDateRangeChange={() => undefined}
      windowSources={storyWindowSources}
    />
  );
}

export function StoryInboxView({ state }: { state: AnalyticsReadState }) {
  const store = useMemo(createStoryInboxStore, []);
  useEffect(() => setAnalyticsStoryState(state), [state]);
  return (
    <InboxStage>
      <OperatorInboxView
        store={store}
        analyticsState={state}
        analyticsWindowSource={storyWindowSources.conversationInsights}
      />
    </InboxStage>
  );
}

export function StoryGraphView({ state }: { state: AnalyticsReadState }) {
  return (
    <GraphExplorerPresentation
      state={state}
      windowSource={storyWindowSources.graph}
      queryGate={{
        enabled: false,
        reason:
          'Query controls are unavailable in this story and in the current product runtime.',
      }}
    />
  );
}

export function StoryView({
  state,
  view,
}: {
  state: AnalyticsReadState;
  view: StoryViewName;
}) {
  if (view === 'analytics') return <StoryAnalyticsView state={state} />;
  if (view === 'inbox') return <StoryInboxView state={state} />;
  if (view === 'graph') return <StoryGraphView state={state} />;
  return <StoryDashboardView state={state} />;
}
