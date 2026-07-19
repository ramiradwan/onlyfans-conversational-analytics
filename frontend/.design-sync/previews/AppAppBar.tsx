import { Box } from '@mui/material';
import { AppAppBar } from 'onlyfans-analytics-frontend';

export function DesktopHeader() {
  return (
    <Box sx={{ height: 88, position: 'relative', width: '100%' }}>
      <AppAppBar drawerWidth={0} onDrawerToggle={() => {}} />
    </Box>
  );
}
