import { Box } from '@mui/material';
import {
  CreatorDashboardView,
  createPreviewActivationApi,
  createPreviewBridgeStore,
} from 'onlyfans-analytics-frontend';

const previewStore = createPreviewBridgeStore();
const previewActivationApi = createPreviewActivationApi();

export function CanonicalSnapshot() {
  return (
    <Box sx={{ height: 760, minWidth: 980 }}>
      <CreatorDashboardView activationApi={previewActivationApi} store={previewStore} />
    </Box>
  );
}
