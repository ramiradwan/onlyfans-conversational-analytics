import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { MessageApi } from '../src/services/messageApi';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';
import OperatorInboxView from '../src/views/OperatorInboxView';

const ACCOUNT_ID = 'creator-account';
const AS_OF = '2026-07-18T13:00:00Z';

const messageApi: MessageApi = {
  getPage: vi.fn(async ({ conversationId }) => ({
    creator_account_id: ACCOUNT_ID,
    conversation_id: conversationId,
    projection_generation: 'projection-generation-7',
    read_revision: 7,
    generated_at: AS_OF,
    items: [{
      message_id: 'recent-message',
      text: 'Recent message',
      sent_at: AS_OF,
      direction: 'inbound',
      sentiment: 'neutral',
    }],
    older_cursor: null,
    has_older_stored_items: false,
    conversation_coverage: {
      status: 'partial',
      boundary: null,
      earliest_available_at: null,
      latest_acquired_at: AS_OF,
      data_as_of: AS_OF,
      reason_code: 'history_boundary_not_observed',
    },
    projection: {
      status: 'current',
      canonical_revision: 7,
      projected_revision: 7,
      projected_at: AS_OF,
      reason: null,
    },
  })),
};

afterEach(() => cleanup());

describe('OperatorInboxView setup state', () => {
  it('keeps recent conversations visible and explains how to include older messages when history is off', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot({
      creator_account_id: ACCOUNT_ID,
      view_revision: 1,
      generated_at: AS_OF,
      conversations: [{
        conversation_id: 'recent-chat',
        platform_user_id: 'fan-recent-chat',
        display_name: 'Recent Fan',
        unread_count: 0,
        last_message_at: AS_OF,
        latest_message: {
          message_id: 'recent-preview',
          text: 'Recent preview',
          sent_at: AS_OF,
          direction: 'inbound',
          sentiment: 'neutral',
        },
        coverage: {
          status: 'partial',
          boundary: null,
          earliest_available_at: null,
          latest_acquired_at: AS_OF,
          data_as_of: AS_OF,
          reason_code: 'history_boundary_not_observed',
        },
      }],
      analytics: {
        total_conversations: {
          value: 1,
          basis: 'synced_subset',
          observed_range: { start: null, end: AS_OF },
          complete_range: null,
          sample_size: 1,
          as_of: AS_OF,
          projection_revision: 7,
        },
        total_messages: {
          value: 1,
          basis: 'synced_subset',
          observed_range: { start: null, end: AS_OF },
          complete_range: null,
          sample_size: 1,
          as_of: AS_OF,
          projection_revision: 7,
        },
        inbound_messages: {
          value: 1,
          basis: 'synced_subset',
          observed_range: { start: null, end: AS_OF },
          complete_range: null,
          sample_size: 1,
          as_of: AS_OF,
          projection_revision: 7,
        },
        outbound_messages: {
          value: 0,
          basis: 'synced_subset',
          observed_range: { start: null, end: AS_OF },
          complete_range: null,
          sample_size: 0,
          as_of: AS_OF,
          projection_revision: 7,
        },
      },
      coverage: {
        status: 'unknown',
        phase: 'not_started',
        generation_id: null,
        as_of: null,
        discovered_conversations: null,
        complete_conversations: 0,
        complete_as_of: null,
        reason: null,
      },
      projection: {
        status: 'current',
        canonical_revision: 7,
        projected_revision: 7,
        projected_at: AS_OF,
        reason: null,
      },
      live_freshness: {
        status: 'current',
        last_observed_at: AS_OF,
        last_committed_at: AS_OF,
        expires_at: '2026-07-18T13:02:00Z',
        pending_count: 0,
        reason: null,
      },
    });

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <OperatorInboxView messageApi={messageApi} store={store} />
      </ThemeProvider>,
    );

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Message history is off');
    expect(alert.textContent).toContain(
      'Recent conversations are shown. Turn on message history in Settings to include older messages.',
    );
    expect(
      within(screen.getByRole('list', { name: 'Conversation list' }))
        .getByRole('button', { name: /Conversation with Recent Fan/ }),
    ).toBeTruthy();
  });
});
