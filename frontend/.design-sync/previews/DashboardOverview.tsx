import { Box } from '@mui/material';
import { DashboardOverview } from 'onlyfans-analytics-frontend';

export function UpToDate() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2 }}>
      <DashboardOverview
        conversations="128"
        messages="2,436"
        received="1,402"
        sent="1,034"
        split={{ received: 1402, sent: 1034 }}
      />
    </Box>
  );
}

export function SyncingHistory() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2 }}>
      <DashboardOverview
        conversations="46"
        messages="812"
        progress={{ label: 'History 36% synced', percent: 36 }}
        received="478"
        sent="334"
      />
    </Box>
  );
}

export function Loading() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2 }}>
      <DashboardOverview conversations="—" isLoading messages="—" received="—" sent="—" />
    </Box>
  );
}
