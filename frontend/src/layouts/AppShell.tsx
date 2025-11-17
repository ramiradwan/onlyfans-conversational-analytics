import React, { useState } from 'react';  
import { Box, Toolbar, CssBaseline, useTheme } from '@mui/material';  
import { Outlet } from 'react-router-dom';  
import { AppAppBar } from './AppAppBar';  
import { AppDrawer } from './AppDrawer';  
  
const DRAWER_WIDTH = 240;  
  
export function AppShell() {  
  const theme = useTheme();  
  const [mobileOpen, setMobileOpen] = useState(false);  
  
  const handleDrawerToggle = () => {  
    setMobileOpen(!mobileOpen);  
  };  
  
  return (  
    <Box sx={{ display: 'flex', height: '100vh' }}>  
      <CssBaseline />  
  
      {/* Top App Bar */}  
      <AppAppBar  
        drawerWidth={DRAWER_WIDTH}  
        onDrawerToggle={handleDrawerToggle}  
      />  
  
      {/* Side Navigation Drawer */}  
      <AppDrawer  
        drawerWidth={DRAWER_WIDTH}  
        mobileOpen={mobileOpen}  
        onDrawerToggle={handleDrawerToggle}  
      />  
  
      {/* Main Content Area */}  
      <Box  
        component="main"  
        role="main"  
        sx={{  
          flexGrow: 1,  
          display: 'flex', // flex container for child views  
          flexDirection: 'column', // vertical stacking  
          p: theme.layout.pagePadding, // tokenised page padding  
          width: { sm: `calc(100% - ${DRAWER_WIDTH}px)` },  
          bgcolor: 'background.default',  
          height: '100vh',  
          boxSizing: 'border-box',  
          overflow: 'hidden', // no outer scroll  
        }}  
      >  
        {/* Spacer to offset fixed AppBar */}  
        <Toolbar />  
  
        {/* Routed view will stretch to fill available space */}  
        <Outlet />  
      </Box>  
    </Box>  
  );  
}  