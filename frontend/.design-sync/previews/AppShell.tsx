import {
  AppShell,
  CreatorDashboardView,
  MemoryRouter,
  Route,
  Routes,
  createPreviewBridgeStore,
  seedPreviewShellStore,
} from 'onlyfans-analytics-frontend';

const previewStore = createPreviewBridgeStore();
seedPreviewShellStore();

export function CreatorWorkspace() {
  return (
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<CreatorDashboardView store={previewStore} />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}
