import { Box, Typography } from '@mui/material';
import { ChartPlaceholder } from 'onlyfans-analytics-frontend';

export function DashboardChart() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 760, p: 2 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Sentiment over time
      </Typography>
      <ChartPlaceholder height={260} />
    </Box>
  );
}

export function CompactChart() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 520, p: 2 }}>
      <ChartPlaceholder height={180} />
    </Box>
  );
}
