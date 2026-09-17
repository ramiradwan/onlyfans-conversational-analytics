import {
  Box,
  MessageStreamPlaceholder,
  Typography,
} from 'onlyfans-analytics-frontend';

export function MessageHistoryLoading() {
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 720, p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Messages
      </Typography>
      <MessageStreamPlaceholder bubbles={5} />
    </Box>
  );
}
