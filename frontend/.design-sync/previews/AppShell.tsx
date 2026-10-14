import {
  AppShell,
  CreatorDashboardView,
  MemoryRouter,
  Route,
  Routes,
  createPreviewActivationApi,
  createPreviewDesktopOnboardingApi,
  createPreviewBridgeStore,
  seedPreviewShellStore,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

const previewStore = createPreviewBridgeStore();
const previewActivationApi = createPreviewActivationApi();
const previewOnboardingApi = createPreviewDesktopOnboardingApi();
seedPreviewShellStore();

export function CreatorWorkspace() {
  return (
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route
            index
            element={<CreatorDashboardView activationApi={previewActivationApi} onboardingApi={previewOnboardingApi} store={previewStore} />}
          />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}
