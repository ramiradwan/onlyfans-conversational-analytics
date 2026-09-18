import {
  Box,
  MessageStreamPlaceholder,
  Typography,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

export function MessageHistoryLoading() {
  return (
    <Box sx={{ maxWidth: 720 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Messages
      </Typography>
      <MessageStreamPlaceholder bubbles={5} />
    </Box>
  );
}
