import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { StateSnapshotPayload } from '../src/protocol';
import {
  storyAnalyticsModel,
  storyBaselineState,
  storyWindowSources,
} from '../src/story-only/analyticsFixtures';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';
import OperatorInboxView from '../src/views/OperatorInboxView';

const AS_OF = '2026-06-30T12:00:00.000Z';

function storyStore() {
  const store = createBridgeTransportStore();
  store.bindAccount('story-only-account');
  const snapshot: StateSnapshotPayload = {
    creator_account_id: 'story-only-account',
    view_revision: 1,
    generated_at: AS_OF,
    conversations: [
      {
        conversation_id: 'story-conversation-one',
        platform_user_id: 'story-participant-one',
        display_name: 'Story Participant',
        unread_count: 2,
        last_message_at: AS_OF,
        latest_message: null,
        coverage: {
          status: 'complete',
          boundary: 'history_start',
          earliest_available_at: '2026-06-01T00:00:00.000Z',
          latest_acquired_at: AS_OF,
          data_as_of: AS_OF,
          reason_code: null,
        },
      },
    ],
    analytics: {
      total_conversations: { value: 1, basis: 'complete', observed_range: { start: null, end: AS_OF }, complete_range: { start: null, end: AS_OF }, sample_size: 1, as_of: AS_OF, projection_revision: 1 },
      total_messages: { value: 0, basis: 'complete', observed_range: { start: null, end: AS_OF }, complete_range: { start: null, end: AS_OF }, sample_size: 0, as_of: AS_OF, projection_revision: 1 },
      inbound_messages: { value: 0, basis: 'complete', observed_range: { start: null, end: AS_OF }, complete_range: { start: null, end: AS_OF }, sample_size: 0, as_of: AS_OF, projection_revision: 1 },
      outbound_messages: { value: 0, basis: 'complete', observed_range: { start: null, end: AS_OF }, complete_range: { start: null, end: AS_OF }, sample_size: 0, as_of: AS_OF, projection_revision: 1 },
    },
    coverage: {
      status: 'complete',
      phase: 'complete',
      generation_id: '90000000-0000-4000-8000-000000000001',
      as_of: AS_OF,
      discovered_conversations: 1,
      complete_conversations: 1,
      complete_as_of: AS_OF,
      reason: null,
    },
    projection: {
      status: 'current',
      canonical_revision: 1,
      projected_revision: 1,
      projected_at: AS_OF,
      reason: null,
    },
    live_freshness: {
      status: 'current',
      last_observed_at: AS_OF,
      last_committed_at: AS_OF,
      expires_at: '2026-06-30T12:02:00.000Z',
      pending_count: 0,
      reason: null,
    },
  };
  store.applySnapshot(snapshot);
  return store;
}

afterEach(() => cleanup());

describe('conversation insights composition', () => {
  it('renders the insights panel alongside the conversation list with unique accessible references', () => {
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <OperatorInboxView
          store={storyStore()}
          analyticsState={storyBaselineState}
          conversationInsight={storyAnalyticsModel.conversations[0]}
          analyticsWindowSource={storyWindowSources.conversationInsights}
        />
      </ThemeProvider>,
    );

    expect(screen.getByRole('region', { name: 'Conversations' })).toBeTruthy();
    const panel = screen.getByRole('complementary', { name: 'Conversation insights' });
    expect(panel).toBeTruthy();
    expect(within(panel).getByText('Story Participant')).toBeTruthy();
    expect(within(panel).getByText('14')).toBeTruthy(); // fixture message count

    const ids = Array.from(document.querySelectorAll<HTMLElement>('[id]')).map(
      (element) => element.id,
    );
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('omits the insights panel when no analytics state is supplied', () => {
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <OperatorInboxView store={storyStore()} />
      </ThemeProvider>,
    );

    expect(screen.queryByRole('complementary')).toBeNull();
  });
});
