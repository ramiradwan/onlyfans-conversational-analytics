import { Box, Typography } from '@mui/material';
import { TablePlaceholder } from 'onlyfans-analytics-frontend';

export function TopicMetricsLoading() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 840, p: 2 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Topic metrics
      </Typography>
      <TablePlaceholder rows={5} />
    </Box>
  );
}

export function CompactTable() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 680, p: 2 }}>
      <TablePlaceholder rows={3} />
    </Box>
  );
}
