import {
  AppDrawer,
  MemoryRouter,
  seedPreviewShellStore,
} from 'onlyfans-analytics-frontend';

seedPreviewShellStore();

// The permanent desktop rail is sm-gated (display xs:none / sm:block) and cannot
// render at the same narrow capture viewport that the open mobile drawer needs;
// the collapsed desktop rail is shown in context by the AppShell preview instead.
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
