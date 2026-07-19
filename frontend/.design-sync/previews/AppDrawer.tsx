import { AppDrawer, MemoryRouter } from 'onlyfans-analytics-frontend';

export function MobileNavigation() {
  return (
    <MemoryRouter initialEntries={['/analytics']}>
      <div style={{ height: 440, position: 'relative', width: 260 }}>
        <AppDrawer drawerWidth={260} mobileOpen onDrawerToggle={() => {}} />
      </div>
    </MemoryRouter>
  );
}
