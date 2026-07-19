import { Box } from '@mui/material';
import type { ConversationSummary } from '../../src/protocol';
import {
  ChatListPane,
  previewConversations,
} from 'onlyfans-analytics-frontend';

const conversationSummaries: ConversationSummary[] = previewConversations.map((conversation) => ({
  conversation_id: conversation.conversation_id,
  platform_user_id: conversation.platform_user_id,
  display_name: conversation.display_name,
  unread_count: conversation.unread_count,
  last_message_at: conversation.last_message_at,
  latest_message: conversation.messages.at(-1) ?? null,
  coverage: {
    status: 'complete',
    boundary: 'history_start',
    earliest_available_at: conversation.messages.at(0)?.sent_at ?? null,
    latest_acquired_at: conversation.last_message_at,
    data_as_of: '2026-07-18T12:00:00.000Z',
    reason_code: null,
  },
}));

export function ConversationList() {
  return (
    <Box sx={{ height: 420, maxWidth: 420 }}>
      <ChatListPane
        conversations={conversationSummaries}
        isLoading={false}
        onSelectConversation={() => {}}
        selectedConversationId="bailey"
      />
    </Box>
  );
}

export function Empty() {
  return (
    <Box sx={{ height: 260, maxWidth: 420 }}>
      <ChatListPane
        conversations={[]}
        isLoading={false}
        onSelectConversation={() => {}}
        selectedConversationId={null}
      />
    </Box>
  );
}

export function Loading() {
  return (
    <Box sx={{ height: 360, maxWidth: 420 }}>
      <ChatListPane
        conversations={[]}
        isLoading
        onSelectConversation={() => {}}
        selectedConversationId={null}
      />
    </Box>
  );
}
