import { Box, Stack, Typography } from '@mui/material';
import { KpiCard } from 'onlyfans-analytics-frontend';

export function DashboardMetrics() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, width: '100%' }}>
      <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 1.5 }}>
        Today at a glance
      </Typography>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <KpiCard title="Total conversations" value={4} grow />
        <KpiCard title="Total messages" value={8} grow />
        <KpiCard title="Inbound messages" value={5} grow />
      </Stack>
    </Box>
  );
}

export function LoadingState() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, maxWidth: 360 }}>
      <KpiCard title="Total messages" value={0} isLoading />
    </Box>
  );
}
