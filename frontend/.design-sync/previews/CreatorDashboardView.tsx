import {
  Box,
  CreatorDashboardView,
  createPreviewActivationApi,
  createPreviewDesktopOnboardingApi,
  createPreviewBridgeStore,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

const previewStore = createPreviewBridgeStore();
const previewActivationApi = createPreviewActivationApi();
const previewOnboardingApi = createPreviewDesktopOnboardingApi();

export function CanonicalSnapshot() {
  return (
    <Box sx={{ height: 760, minWidth: 980 }}>
      <CreatorDashboardView activationApi={previewActivationApi} onboardingApi={previewOnboardingApi} store={previewStore} />
    </Box>
  );
}
