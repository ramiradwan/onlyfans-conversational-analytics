import { Box } from '@mui/material';
import {
  CreatorDashboardView,
  createPreviewBridgeStore,
} from 'onlyfans-analytics-frontend';

const previewStore = createPreviewBridgeStore();

export function CanonicalSnapshot() {
  return (
    <Box sx={{ height: 760, minWidth: 980 }}>
      <CreatorDashboardView store={previewStore} />
    </Box>
  );
}
