import {
  AppDrawer,
  MemoryRouter,
  seedPreviewShellStore,
} from 'onlyfans-analytics-frontend';

seedPreviewShellStore();

export function DesktopNavigation() {
  return (
    <MemoryRouter initialEntries={['/']}>
      <div style={{ height: 440, position: 'relative', width: 76 }}>
        <AppDrawer drawerWidth={76} mobileOpen={false} />
      </div>
    </MemoryRouter>
  );
}

export function MobileNavigation() {
  return (
    <MemoryRouter initialEntries={['/analytics']}>
      <div style={{ height: 440, position: 'relative', width: 264 }}>
        <AppDrawer
          drawerWidth={76}
          mobileDrawerWidth={264}
          mobileOpen
          onDrawerClose={() => {}}
        />
      </div>
    </MemoryRouter>
  );
}
