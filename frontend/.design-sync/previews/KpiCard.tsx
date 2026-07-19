import { Box, Stack, Typography } from '@mui/material';
import { KpiCard } from 'onlyfans-analytics-frontend';

export function DashboardMetrics() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, width: '100%' }}>
      <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 1.5 }}>
        Today at a glance
      </Typography>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <KpiCard title="Avg. response time" value="4.8 min" grow />
        <KpiCard title="Overall sentiment" value="84%" grow />
        <KpiCard title="Unread messages" value={27} grow />
      </Stack>
    </Box>
  );
}

export function LoadingState() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, maxWidth: 360 }}>
      <KpiCard title="Revenue today" value="€1,284" isLoading />
    </Box>
  );
}
