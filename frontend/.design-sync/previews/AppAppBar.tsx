import {
  AppAppBar,
  Box,
  MemoryRouter,
  seedPreviewShellStore,
} from 'onlyfans-analytics-frontend';

seedPreviewShellStore();

export function LiveDesktopHeader() {
  return (
    <MemoryRouter initialEntries={['/']}>
      <Box sx={{ height: 88, position: 'relative', width: '100%' }}>
        <AppAppBar headerHeight={72} onDrawerToggle={() => {}} />
      </Box>
    </MemoryRouter>
  );
}
