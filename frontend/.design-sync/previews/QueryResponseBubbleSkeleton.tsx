import { Box, Typography } from '@mui/material';
import { QueryResponseBubbleSkeleton } from 'onlyfans-analytics-frontend';

export function GeneratingAnswer() {
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 640, p: 2 }}>
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
