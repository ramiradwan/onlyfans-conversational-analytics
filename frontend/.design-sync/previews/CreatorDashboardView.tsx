import { Box } from '@mui/material';
import {
  CreatorDashboardView,
  seedPreviewAnalytics,
} from 'onlyfans-analytics-frontend';

export function LoadedDashboard() {
  seedPreviewAnalytics();
  return (
    <Box sx={{ minWidth: 980 }}>
      <CreatorDashboardView />
    </Box>
  );
}
