import { Box, Stack, Typography } from '@mui/material';
import { KpiCardSkeleton } from 'onlyfans-analytics-frontend';

export function DashboardRow() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, width: '100%' }}>
      <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 1.5 }}>
        Loading dashboard metrics
      </Typography>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <KpiCardSkeleton grow />
        <KpiCardSkeleton grow />
        <KpiCardSkeleton grow />
      </Stack>
    </Box>
  );
}
