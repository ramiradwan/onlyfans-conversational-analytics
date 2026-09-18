import {
  Box,
  CreatorDashboardView,
  createPreviewActivationApi,
  createPreviewBridgeStore,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

const previewStore = createPreviewBridgeStore();
const previewActivationApi = createPreviewActivationApi();

export function CanonicalSnapshot() {
  return (
    <Box sx={{ height: 760, minWidth: 980 }}>
      <CreatorDashboardView activationApi={previewActivationApi} store={previewStore} />
    </Box>
  );
}
