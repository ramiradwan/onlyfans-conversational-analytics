import { describe, expect, it } from 'vitest';

import type {
  AnalyticsMetric,
  AnalyticsView,
  ConversationSummary,
  MessageView,
} from '../src/protocol';
import {
  buildCreatorDashboardModel,
  buildMessageActivity,
  buildSentimentBreakdown,
  createCreatorDashboardModel,
  deriveMostActiveConversations,
  deriveSentimentCounts,
  deriveUtcMessageActivity,
  flattenMessages,
  rankActiveConversations,
} from '../src/views/creatorDashboardModel';

const AS_OF = '2026-07-19T12:00:00Z';

function message(
  messageId: string,
  sentAt: string,
  direction: MessageView['direction'] = 'inbound',
  sentiment: MessageView['sentiment'] = 'neutral',
): MessageView {
  return {
    message_id: messageId,
    text: `Preview ${messageId}`,
    sent_at: sentAt,
    direction,
    sentiment,
  };
}

function conversation(
  conversationId: string,
  {
    displayName = conversationId,
    lastMessageAt = null,
    latestMessage = null,
    platformUserId = `fan-${conversationId}`,
  }: {
    displayName?: string | null;
    lastMessageAt?: string | null;
    latestMessage?: MessageView | null;
    platformUserId?: string | null;
  } = {},
): ConversationSummary {
  return {
    conversation_id: conversationId,
    platform_user_id: platformUserId,
    display_name: displayName,
    unread_count: 0,
    last_message_at: lastMessageAt,
    latest_message: latestMessage,
    coverage: {
      status: 'partial',
      boundary: null,
      earliest_available_at: null,
      latest_acquired_at: lastMessageAt,
      data_as_of: AS_OF,
      reason_code: 'history_boundary_not_observed',
    },
  };
}

function metric(value: number, basis: AnalyticsMetric['basis']): AnalyticsMetric {
  return {
    value,
    basis,
    observed_range: { start: '2026-07-01T00:00:00Z', end: AS_OF },
    complete_range:
      basis === 'complete'
        ? { start: '2026-07-01T00:00:00Z', end: AS_OF }
        : null,
    sample_size: 19,
    as_of: AS_OF,
    projection_revision: 8,
  };
}

describe('creator dashboard bounded-summary model', () => {
  it('never treats a one-message WebSocket preview as historical message data', () => {
    const conversations = [
      conversation('alpha', {
        lastMessageAt: '2026-07-19T11:00:00Z',
        latestMessage: message(
          'alpha-preview',
          '2026-07-19T11:00:00Z',
          'outbound',
          'positive',
        ),
      }),
    ];

    expect(flattenMessages(conversations)).toEqual([]);
    expect(deriveUtcMessageActivity(conversations)).toEqual([]);
    expect(deriveSentimentCounts(conversations)).toEqual([
      { sentiment: 'positive', label: 'Positive', count: 0 },
      { sentiment: 'neutral', label: 'Neutral', count: 0 },
      { sentiment: 'negative', label: 'Negative', count: 0 },
      { sentiment: 'unknown', label: 'Unknown', count: 0 },
    ]);
  });

  it('orders summary-only conversations by latest activity with deterministic ID ties', () => {
    const conversations = [
      conversation('zulu', {
        displayName: null,
        lastMessageAt: '2026-07-19T10:00:00Z',
        platformUserId: 'fan-zulu',
      }),
      conversation('bravo', { lastMessageAt: '2026-07-19T11:00:00Z' }),
      conversation('alpha', { lastMessageAt: '2026-07-19T11:00:00Z' }),
      conversation('latest-preview', {
        lastMessageAt: '2026-07-19T09:00:00Z',
        latestMessage: message('newer-preview', '2026-07-19T12:00:00Z'),
        platformUserId: null,
      }),
    ];

    expect(deriveMostActiveConversations(conversations, 4)).toEqual([
      {
        conversationId: 'latest-preview',
        displayName: 'latest-preview',
        platformUserId: 'latest-preview',
        messageCount: 0,
        inboundCount: 0,
        outboundCount: 0,
        lastMessageAt: '2026-07-19T12:00:00Z',
      },
      {
        conversationId: 'alpha',
        displayName: 'alpha',
        platformUserId: 'fan-alpha',
        messageCount: 0,
        inboundCount: 0,
        outboundCount: 0,
        lastMessageAt: '2026-07-19T11:00:00Z',
      },
      {
        conversationId: 'bravo',
        displayName: 'bravo',
        platformUserId: 'fan-bravo',
        messageCount: 0,
        inboundCount: 0,
        outboundCount: 0,
        lastMessageAt: '2026-07-19T11:00:00Z',
      },
      {
        conversationId: 'zulu',
        displayName: 'fan-zulu',
        platformUserId: 'fan-zulu',
        messageCount: 0,
        inboundCount: 0,
        outboundCount: 0,
        lastMessageAt: '2026-07-19T10:00:00Z',
      },
    ]);
  });

  it('keeps compatibility helpers on the bounded-summary behavior', () => {
    const conversations = [
      conversation('alpha', {
        latestMessage: message('preview', AS_OF, 'inbound', 'unknown'),
      }),
    ];

    expect(buildMessageActivity(conversations)).toEqual(
      deriveUtcMessageActivity(conversations),
    );
    expect(buildSentimentBreakdown(conversations)).toEqual(
      deriveSentimentCounts(conversations),
    );
    expect(rankActiveConversations(conversations)).toEqual(
      deriveMostActiveConversations(conversations),
    );
  });

  it('preserves every analytics evidence envelope without deriving totals from previews', () => {
    const analytics: AnalyticsView = {
      total_conversations: metric(7, 'complete'),
      total_messages: metric(19, 'synced_subset'),
      inbound_messages: metric(11, 'synced_subset'),
      outbound_messages: metric(8, 'synced_subset'),
    };
    const conversations = [
      conversation('alpha', {
        lastMessageAt: AS_OF,
        latestMessage: message('preview', AS_OF),
      }),
    ];

    const model = buildCreatorDashboardModel(analytics, conversations);
    expect(model.analytics).toEqual(analytics);
    expect(model.messageActivity).toEqual([]);
    expect(model.sentimentCounts.every(({ count }) => count === 0)).toBe(true);
    expect(model.mostActiveConversations[0]).toMatchObject({
      conversationId: 'alpha',
      messageCount: 0,
      inboundCount: 0,
      outboundCount: 0,
    });
    expect(createCreatorDashboardModel({ analytics, conversations })).toEqual(model);
  });

  it('represents the pre-snapshot analytics model as unavailable', () => {
    expect(buildCreatorDashboardModel(null, [])).toEqual({
      analytics: null,
      messageActivity: [],
      sentimentCounts: [
        { sentiment: 'positive', label: 'Positive', count: 0 },
        { sentiment: 'neutral', label: 'Neutral', count: 0 },
        { sentiment: 'negative', label: 'Negative', count: 0 },
        { sentiment: 'unknown', label: 'Unknown', count: 0 },
      ],
      mostActiveConversations: [],
    });
  });
});
