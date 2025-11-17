import AnalyticsIcon from '@mui/icons-material/Analytics';  
import DashboardIcon from '@mui/icons-material/Dashboard';  
import InboxIcon from '@mui/icons-material/Inbox';  
import TravelExploreIcon from '@mui/icons-material/TravelExplore';  
import {  
  Box,  
  Drawer,  
  List,  
  ListItem,  
  ListItemButton,  
  ListItemIcon,  
  ListItemText,  
  Toolbar,  
  useTheme,  
} from '@mui/material';  
import React from 'react';  
import { NavLink, useLocation } from 'react-router-dom';  
import { usePermissions } from '@hooks/usePermissions';  
  
interface AppDrawerProps {  
  drawerWidth: number;  
  mobileOpen: boolean;  
  onDrawerToggle: () => void;  
}  
  
/** Sidebar navigation item with active state styling. */  
function SidebarNavItem({  
  to,  
  icon,  
  text,  
}: {  
  to: string;  
  icon: React.ReactNode;  
  text: string;  
}) {  
  const location = useLocation();  
  const theme = useTheme();  
  const isActive =  
    to === '/' ? location.pathname === '/' : location.pathname.startsWith(to);  
  
  return (  
    <ListItem disablePadding>  
      <ListItemButton  
        component={NavLink}  
        to={to}  
        selected={isActive}  
        sx={{  
          '&.Mui-selected': {  
            fontWeight: 700,  
            bgcolor: 'action.selected',  
            '& .MuiListItemIcon-root': {  
              color: 'primary.main',  
            },  
          },  
          '&:focus-visible': {  
            outline: `2px solid ${theme.vars.palette.primary.main}`,  
          },  
        }}  
      >  
        <ListItemIcon>{icon}</ListItemIcon>  
        <ListItemText primary={text} />  
      </ListItemButton>  
    </ListItem>  
  );  
}  
  
export function AppDrawer({  
  drawerWidth,  
  mobileOpen,  
  onDrawerToggle,  
}: AppDrawerProps) {  
  const theme = useTheme();  
  const {  
    canViewAnalytics,  
    canViewInbox,  
    canViewDashboard,  
    canViewGraphExplorer,  
  } = usePermissions();  
  
  const drawerContent = (  
    <div role="navigation" aria-label="Main navigation">  
      <Toolbar /> {/* Spacer to align with AppBar */}  
      <List>  
        {canViewDashboard && (  
          <SidebarNavItem to="/" icon={<DashboardIcon />} text="Dashboard" />  
        )}  
        {canViewInbox && (  
          <SidebarNavItem to="/inbox" icon={<InboxIcon />} text="Inbox" />  
        )}  
        {canViewAnalytics && (  
          <SidebarNavItem  
            to="/analytics"  
            icon={<AnalyticsIcon />}  
            text="Analytics"  
          />  
        )}  
        {canViewGraphExplorer && (  
          <SidebarNavItem  
            to="/graph-explorer"  
            icon={<TravelExploreIcon />}  
            text="Graph Explorer"  
          />  
        )}  
      </List>  
    </div>  
  );  
  
  return (  
    <Box  
      component="nav"  
      sx={{ width: { sm: drawerWidth }, flexShrink: { sm: 0 } }}  
      aria-label="application navigation"  
    >  
      {/* Mobile Drawer */}  
      <Drawer  
        variant="temporary"  
        open={mobileOpen}  
        onClose={onDrawerToggle}  
        ModalProps={{ keepMounted: true }}  
        sx={{  
          display: { xs: 'block', sm: 'none' },  
          '& .MuiDrawer-paper': {  
            boxSizing: 'border-box',  
            width: drawerWidth,  
            bgcolor: 'background.paper',  
            ...theme.effects.sideBorder(theme),  
          },  
        }}  
      >  
        {drawerContent}  
      </Drawer>  
  
      {/* Desktop Drawer */}  
      <Drawer  
        variant="permanent"  
        sx={{  
          display: { xs: 'none', sm: 'block' },  
          '& .MuiDrawer-paper': {  
            boxSizing: 'border-box',  
            width: drawerWidth,  
            bgcolor: 'background.paper',  
            ...theme.effects.sideBorder(theme),  
          },  
        }}  
        open  
      >  
        {drawerContent}  
      </Drawer>  
    </Box>  
  );  
}  