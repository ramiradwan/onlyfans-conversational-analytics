import { Box, Typography } from '@mui/material';
import {
  AppAppBar,
  AppDrawer,
  MemoryRouter,
} from 'onlyfans-analytics-frontend';

export function ApplicationChrome() {
  return (
    <MemoryRouter initialEntries={['/']}>
      <Box sx={{ bgcolor: 'background.default', display: 'flex', height: 560, minWidth: 900 }}>
        <AppAppBar drawerWidth={240} onDrawerToggle={() => {}} />
        <AppDrawer drawerWidth={240} mobileOpen={false} onDrawerToggle={() => {}} />
        <Box component="main" sx={{ flexGrow: 1, ml: '240px', p: 3, pt: 11 }}>
          <Typography variant="h4" gutterBottom>
            Creator Dashboard
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Persistent navigation and calm analytics surfaces frame every workspace.
          </Typography>
        </Box>
      </Box>
    </MemoryRouter>
  );
}
