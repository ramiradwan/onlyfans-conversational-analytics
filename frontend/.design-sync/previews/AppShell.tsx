import {
  AppShell,
  CreatorDashboardView,
  MemoryRouter,
  Route,
  Routes,
  createPreviewActivationApi,
  createPreviewBridgeStore,
  seedPreviewShellStore,
} from 'onlyfans-analytics-frontend';

const previewStore = createPreviewBridgeStore();
const previewActivationApi = createPreviewActivationApi();
seedPreviewShellStore();

export function CreatorWorkspace() {
  return (
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route
            index
            element={<CreatorDashboardView activationApi={previewActivationApi} store={previewStore} />}
          />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}
