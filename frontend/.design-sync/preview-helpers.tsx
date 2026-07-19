import { Box, Stack, Typography } from '@mui/material';
import type { ReactNode } from 'react';

import type { ConversationView, MessageView, StateSnapshotPayload } from '../src/protocol';
import { analyticsStoreActions } from '../src/store/analyticsStore';
import { createBridgeTransportStore } from '../src/store/transportStore';

export const previewNoop = () => {};

export function seedPreviewAnalytics() {
  analyticsStoreActions.handleAnalyticsUpdate({
    topics: [
      { topic: 'Behind the scenes', volume: 184, percentage_of_total: 0.31, trend: 0.14 },
      { topic: 'Custom content', volume: 149, percentage_of_total: 0.25, trend: 0.09 },
      { topic: 'Fitness routines', volume: 112, percentage_of_total: 0.19, trend: 0.04 },
      { topic: 'Travel', volume: 86, percentage_of_total: 0.14, trend: -0.02 },
      { topic: 'Live sessions', volume: 65, percentage_of_total: 0.11, trend: 0.06 },
    ],
    sentiment_trend: {
      trend: [
        { date: '2026-07-12', sentiment_score: 0.68 },
        { date: '2026-07-13', sentiment_score: 0.71 },
        { date: '2026-07-14', sentiment_score: 0.73 },
        { date: '2026-07-15', sentiment_score: 0.78 },
        { date: '2026-07-16', sentiment_score: 0.76 },
        { date: '2026-07-17', sentiment_score: 0.82 },
        { date: '2026-07-18', sentiment_score: 0.84 },
      ],
    },
    response_time_metrics: {
      average_handling_time_minutes: 4.8,
      silence_percentage: 12,
      turns: 386,
    },
    priorityScores: {
      bailey: 0.92,
      alex: 0.78,
      casey: 0.66,
    },
    unreadCounts: {
      bailey: 3,
      alex: 1,
      casey: 0,
    },
  });
}

export function PreviewFrame({
  children,
  maxWidth = 960,
  minHeight,
}: {
  children: ReactNode;
  maxWidth?: number | string;
  minHeight?: number | string;
}) {
  return (
    <Box
      sx={{
        bgcolor: 'background.default',
        boxSizing: 'border-box',
        color: 'text.primary',
        maxWidth,
        minHeight,
        p: 2,
        width: '100%',
      }}
    >
      {children}
    </Box>
  );
}

export function PreviewLabel({ children }: { children: ReactNode }) {
  return (
    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
      {children}
    </Typography>
  );
}

export function PreviewStack({ children }: { children: ReactNode }) {
  return (
    <Stack spacing={2} sx={{ width: '100%' }}>
      {children}
    </Stack>
  );
}

export function message(
  messageId: string,
  text: string,
  sentAt: string,
  direction: MessageView['direction'] = 'inbound',
  sentiment: MessageView['sentiment'] = 'neutral',
): MessageView {
  return {
    message_id: messageId,
    text,
    sent_at: sentAt,
    direction,
    sentiment,
  };
}

export function conversation(
  conversationId: string,
  displayName: string,
  lastMessageAt: string,
  messages: MessageView[],
  unreadCount = 0,
): ConversationView {
  return {
    conversation_id: conversationId,
    platform_user_id: `fan-${conversationId}`,
    display_name: displayName,
    unread_count: unreadCount,
    last_message_at: lastMessageAt,
    messages,
  };
}

export const previewConversations: ConversationView[] = [
  conversation(
    'bailey',
    'Bailey Hart',
    '2026-07-18T10:00:00.000Z',
    [
      message(
        'bailey-1',
        'That behind-the-scenes set was exactly what I hoped for.',
        '2026-07-18T09:58:00.000Z',
        'inbound',
        'positive',
      ),
      message(
        'bailey-2',
        'I am glad you liked it — there is another set coming Friday.',
        '2026-07-18T10:00:00.000Z',
        'outbound',
        'positive',
      ),
    ],
    3,
  ),
  conversation(
    'alex',
    'Alex River',
    '2026-07-18T09:42:00.000Z',
    [
      message(
        'alex-1',
        'Could you share the full workout routine next week?',
        '2026-07-18T09:42:00.000Z',
        'inbound',
        'neutral',
      ),
    ],
    1,
  ),
  conversation(
    'casey',
    'Casey Lane',
    '2026-07-18T08:17:00.000Z',
    [
      message(
        'casey-1',
        'The download link is not opening for me.',
        '2026-07-18T08:17:00.000Z',
        'inbound',
        'negative',
      ),
    ],
  ),
];

export function createPreviewInboxStore() {
  const store = createBridgeTransportStore();
  const snapshot: StateSnapshotPayload = {
    creator_account_id: 'preview-creator',
    view_revision: 7,
    generated_at: '2026-07-18T12:00:00.000Z',
    conversations: previewConversations,
    analytics: {
      total_conversations: previewConversations.length,
      total_messages: previewConversations.reduce(
        (total, current) => total + current.messages.length,
        0,
      ),
      inbound_messages: 3,
      outbound_messages: 1,
    },
  };

  store.bindAccount(snapshot.creator_account_id);
  store.applySnapshot(snapshot);
  store.setPresence({
    creator_account_id: snapshot.creator_account_id,
    freshness: 'current',
    online_platform_user_ids: ['fan-bailey'],
    server_received_at: '2026-07-18T12:00:00.000Z',
    expires_at: '2026-07-18T12:05:00.000Z',
    last_observation: {
      observation_id: 12,
      observed_at: '2026-07-18T12:00:00.000Z',
    },
  });
  return store;
}
