import {
  Box,
  ChatListPlaceholder,
  Typography,
} from 'onlyfans-analytics-frontend';

export function ConversationsLoading() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 420, p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Conversations
      </Typography>
      <ChatListPlaceholder rows={5} />
    </Box>
  );
}
