import {
  Box,
  QueryResponseBubbleSkeleton,
  Typography,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

export function GeneratingAnswer() {
  return (
    <Box sx={{ maxWidth: 640 }}>
      <Typography
        variant="caption"
        sx={{
          color: 'text.secondary',
          display: 'block',
          mb: 1
        }}>
        Analyzing conversation data…
      </Typography>
      <QueryResponseBubbleSkeleton />
    </Box>
  );
}
