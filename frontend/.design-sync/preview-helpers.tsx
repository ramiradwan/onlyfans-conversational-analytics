import { Box, Stack, Typography } from '@mui/material';
import type { ReactNode } from 'react';

import type {
  AgentStatePayload,
  BridgeSessionPayload,
  AnalyticsMetric,
  ConversationSummary,
  MessageView,
  StateSnapshotPayload,
  SystemStatePayload,
} from '../src/protocol';
import { setAnalyticsStoryState } from '../src/store/analyticsStore';
import {
  bridgeTransportStore,
  createBridgeTransportStore,
  type BridgeTransportStore,
} from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { storyModelState } from '../src/story-only/analyticsFixtures';

export const previewNoop = () => {};

export function seedPreviewAnalytics() {
  setAnalyticsStoryState(storyModelState);
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
    <Typography
      variant="caption"
      sx={{
        color: 'text.secondary',
        display: 'block',
        mb: 1
      }}>
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

interface PreviewConversation extends ConversationSummary {
  messages: MessageView[];
}

export function conversation(
  conversationId: string,
  displayName: string,
  lastMessageAt: string,
  messages: MessageView[],
  unreadCount = 0,
): PreviewConversation {
  return {
    conversation_id: conversationId,
    platform_user_id: `fan-${conversationId}`,
    display_name: displayName,
    unread_count: unreadCount,
    last_message_at: lastMessageAt,
    latest_message: messages.at(-1) ?? null,
    coverage: {
      status: 'complete',
      boundary: 'history_start',
      earliest_available_at: messages.at(0)?.sent_at ?? null,
      latest_acquired_at: lastMessageAt,
      data_as_of: '2026-07-18T12:00:00.000Z',
      reason_code: null,
    },
    messages,
  };
}

export const previewConversations: PreviewConversation[] = [
  conversation(
    'bailey',
    'Bailey Hart',
    '2026-07-18T10:00:00.000Z',
    [
      message(
        'bailey-1',
        'That behind-the-scenes set was exactly what I hoped for.',
        '2026-07-12T09:58:00.000Z',
        'inbound',
        'positive',
      ),
      message(
        'bailey-2',
        'I am glad you liked it — there is another set coming Friday.',
        '2026-07-13T10:00:00.000Z',
        'outbound',
        'positive',
      ),
      message(
        'bailey-3',
        'Friday works for me. Please send a reminder when it is ready.',
        '2026-07-15T10:00:00.000Z',
        'inbound',
        'neutral',
      ),
      message(
        'bailey-4',
        'Absolutely — I have added it to the release note.',
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
    '2026-07-17T09:42:00.000Z',
    [
      message(
        'alex-1',
        'Could you share the full workout routine next week?',
        '2026-07-14T09:42:00.000Z',
        'inbound',
        'neutral',
      ),
      message(
        'alex-2',
        'Yes. I will publish the full routine with the next update.',
        '2026-07-17T09:42:00.000Z',
        'outbound',
        'positive',
      ),
    ],
    1,
  ),
  conversation(
    'casey',
    'Casey Lane',
    '2026-07-16T08:17:00.000Z',
    [
      message(
        'casey-1',
        'The download link is not opening for me.',
        '2026-07-16T08:17:00.000Z',
        'inbound',
        'negative',
      ),
    ],
  ),
  conversation(
    'devon',
    'Devon Lee',
    '2026-07-18T11:24:00.000Z',
    [
      message(
        'devon-1',
        'Is the live session still scheduled for tonight?',
        '2026-07-18T11:24:00.000Z',
        'inbound',
        'unknown',
      ),
    ],
  ),
];

const previewCreatorAccountId = 'preview-creator';

const previewSession: BridgeSessionPayload = {
  connection_id: '00000000-0000-4000-8000-000000000017',
  bridge_session_id: '00000000-0000-4000-8000-000000000018',
  creator_account_id: previewCreatorAccountId,
  negotiated_protocol_version: '2',
  server_version: 'preview',
};

const previewSystem: SystemStatePayload = {
  creator_account_id: previewCreatorAccountId,
  processing_mode: 'realtime',
  readiness: 'ready',
  updated_at: '2026-07-18T12:00:00.000Z',
  detail: 'Canonical preview snapshot is current.',
};

const previewAgent: AgentStatePayload = {
  creator_account_id: previewCreatorAccountId,
  status: 'connected',
  agent_installation_id: '00000000-0000-4000-8000-000000000019',
  connection_id: '00000000-0000-4000-8000-000000000020',
  required_config_revision: 'preview-r7',
  applied_config_revision: 'preview-r7',
  required_history_settings_revision: 7,
  applied_history_settings_revision: 7,
  last_heartbeat_at: '2026-07-18T12:00:00.000Z',
  degraded_reason: null,
};

const metric = (value: number): AnalyticsMetric => ({
  value,
  basis: 'complete',
  observed_range: {
    start: '2026-07-12T09:58:00.000Z',
    end: '2026-07-18T11:24:00.000Z',
  },
  complete_range: {
    start: '2026-07-12T09:58:00.000Z',
    end: '2026-07-18T11:24:00.000Z',
  },
  sample_size: value,
  as_of: '2026-07-18T12:00:00.000Z',
  projection_revision: 7,
});

const previewSnapshot: StateSnapshotPayload = {
  creator_account_id: previewCreatorAccountId,
  view_revision: 7,
  generated_at: '2026-07-18T12:00:00.000Z',
  conversations: previewConversations,
  analytics: {
    total_conversations: metric(previewConversations.length),
    total_messages: metric(
      previewConversations.reduce(
        (total, current) => total + current.messages.length,
        0,
      ),
    ),
    inbound_messages: metric(5),
    outbound_messages: metric(3),
  },
  coverage: {
    status: 'complete',
    phase: 'complete',
    generation_id: '00000000-0000-4000-8000-000000000021',
    as_of: '2026-07-18T12:00:00.000Z',
    discovered_conversations: previewConversations.length,
    complete_conversations: previewConversations.length,
    complete_as_of: '2026-07-18T12:00:00.000Z',
    reason: null,
  },
  projection: {
    status: 'current',
    canonical_revision: 12,
    projected_revision: 12,
    projected_at: '2026-07-18T12:00:00.000Z',
    reason: null,
  },
  live_freshness: {
    status: 'current',
    last_observed_at: '2026-07-18T11:59:59.000Z',
    last_committed_at: '2026-07-18T12:00:00.000Z',
    expires_at: '2026-07-18T12:05:00.000Z',
    pending_count: 0,
    reason: null,
  },
};

export function createPreviewBridgeStore(): BridgeTransportStore {
  const store = createBridgeTransportStore();
  store.bindAccount(previewCreatorAccountId);
  store.acceptSession(previewSession);
  store.applySnapshot(previewSnapshot);
  store.setAgent(previewAgent);
  store.setSystem(previewSystem);
  store.setPresence({
    creator_account_id: previewCreatorAccountId,
    freshness: 'current',
    online_platform_user_ids: ['fan-bailey', 'fan-devon'],
    server_received_at: '2026-07-18T12:00:00.000Z',
    expires_at: '2026-07-18T12:05:00.000Z',
    last_observation: {
      observation_id: 12,
      observed_at: '2026-07-18T12:00:00.000Z',
    },
  });
  return store;
}

export const createPreviewInboxStore = createPreviewBridgeStore;

let previewShellSeeded = false;

export function seedPreviewShellStore() {
  if (previewShellSeeded) return;
  previewShellSeeded = true;
  useUserStore.getState().actions.setUserRole('creator-ceo');
  bridgeTransportStore.reset();
  bridgeTransportStore.bindAccount(previewCreatorAccountId);
  bridgeTransportStore.acceptSession(previewSession);
  bridgeTransportStore.applySnapshot(previewSnapshot);
  bridgeTransportStore.setAgent(previewAgent);
  bridgeTransportStore.setSystem(previewSystem);
}
