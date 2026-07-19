import { Box } from '@mui/material';
import {
  ChatListPane,
  previewConversations,
} from 'onlyfans-analytics-frontend';

export function ConversationList() {
  return (
    <Box sx={{ height: 420, maxWidth: 420 }}>
      <ChatListPane
        conversations={previewConversations}
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
