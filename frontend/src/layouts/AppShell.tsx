import { Box } from '@mui/material';
import { useCallback, useState } from 'react';
import { Outlet } from 'react-router-dom';

import { componentTokens } from '@/theme';

import { AppAppBar } from './AppAppBar';
import { AppDrawer } from './AppDrawer';

const { desktopRailWidth, headerHeight, mobileDrawerWidth, railInset } = componentTokens.shell;

export function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false);

  const handleDrawerToggle = useCallback(() => {
    setMobileOpen((isOpen) => !isOpen);
  }, []);

  const handleDrawerClose = useCallback(() => {
    setMobileOpen(false);
  }, []);

  return (
    <Box sx={{ bgcolor: 'background.default', display: 'flex', height: '100dvh', minHeight: 0 }}>
      <AppAppBar headerHeight={headerHeight} onDrawerToggle={handleDrawerToggle} />

      <AppDrawer
        drawerWidth={desktopRailWidth}
        headerHeight={headerHeight}
        mobileDrawerWidth={mobileDrawerWidth}
        mobileOpen={mobileOpen}
        onDrawerClose={handleDrawerClose}
      />

      <Box
        component="main"
        id="main-content"
        sx={{
          display: 'flex',
          flex: 1,
          flexDirection: 'column',
          height: '100dvh',
          minHeight: 0,
          minWidth: 0,
          overflow: 'hidden',
          pt: `${headerHeight}px`,
          width: { sm: `calc(100% - ${desktopRailWidth + railInset}px)` },
        }}
      >
        <Box
          sx={{
            display: 'flex',
            flex: 1,
            flexDirection: 'column',
            minHeight: 0,
            minWidth: 0,
            overflow: 'hidden',
            pb: { xs: 2, sm: 1.5 },
            pt: { xs: 1, sm: `${railInset}px` },
            px: { xs: 2, sm: 3, lg: 4 },
          }}
        >
          <Outlet />
        </Box>
      </Box>
    </Box>
  );
}
