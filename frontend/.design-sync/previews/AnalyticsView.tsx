import {
  AnalyticsView,
  Box,
  seedPreviewAnalytics,
} from 'onlyfans-analytics-frontend';

export function LoadedDashboard() {
  seedPreviewAnalytics();
  return (
    <Box sx={{ height: 760, minWidth: 980 }}>
      <AnalyticsView />
    </Box>
  );
}
