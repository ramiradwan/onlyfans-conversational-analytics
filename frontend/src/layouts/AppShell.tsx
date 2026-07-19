import { Box, CssBaseline, useTheme } from '@mui/material';
import { useCallback, useState } from 'react';
import { Outlet } from 'react-router-dom';

import { componentTokens } from '@/theme';

import { AppAppBar } from './AppAppBar';
import { AppDrawer } from './AppDrawer';

const { desktopRailWidth, headerHeight, mobileDrawerWidth } = componentTokens.shell;

export function AppShell() {
  const theme = useTheme();
  const [mobileOpen, setMobileOpen] = useState(false);

  const handleDrawerToggle = useCallback(() => {
    setMobileOpen((isOpen) => !isOpen);
  }, []);

  const handleDrawerClose = useCallback(() => {
    setMobileOpen(false);
  }, []);

  return (
    <Box sx={{ display: 'flex', height: '100dvh', minHeight: 0 }}>
      <CssBaseline />

      <AppAppBar
        drawerWidth={desktopRailWidth}
        headerHeight={headerHeight}
        onDrawerToggle={handleDrawerToggle}
      />

      <AppDrawer
        drawerWidth={desktopRailWidth}
        mobileDrawerWidth={mobileDrawerWidth}
        mobileOpen={mobileOpen}
        onDrawerClose={handleDrawerClose}
      />

      <Box
        component="main"
        id="main-content"
        sx={{
          bgcolor: 'background.default',
          display: 'flex',
          flex: 1,
          flexDirection: 'column',
          height: '100dvh',
          minHeight: 0,
          minWidth: 0,
          overflow: 'hidden',
          pt: `${headerHeight}px`,
          width: { sm: `calc(100% - ${desktopRailWidth}px)` },
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
            p: {
              xs: theme.spacing(2),
              sm: theme.spacing(2.5),
              lg: theme.spacing(3),
            },
          }}
        >
          <Outlet />
        </Box>
      </Box>
    </Box>
  );
}
