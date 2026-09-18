import AnalyticsIcon from '@mui/icons-material/Analytics';
import DashboardIcon from '@mui/icons-material/Dashboard';
import InboxIcon from '@mui/icons-material/Inbox';
import SettingsOutlinedIcon from '@mui/icons-material/SettingsOutlined';
import {
  Box,
  Drawer,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Tooltip,
  useTheme,
} from '@mui/material';
import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';

import { componentTokens } from '@/theme';
import { usePermissions } from '@hooks/usePermissions';

import { BrandMark } from './BrandMark';

interface AppDrawerProps {
  drawerWidth: number;
  headerHeight?: number;
  mobileDrawerWidth?: number;
  mobileOpen: boolean;
  onDrawerClose?: () => void;
  onDrawerToggle?: () => void;
}

interface NavigationItem {
  icon: ReactNode;
  label: string;
  to: string;
}

const RAIL_ITEM_SIZE = 44;
const RAIL_ITEM_INSET = componentTokens.MuiPaper.borderRadius - componentTokens.MuiListItemButton.borderRadius;
const RAIL_ITEM_WIDTH = componentTokens.shell.desktopRailWidth - 2 * RAIL_ITEM_INSET;

function DrawerNavItem({
  item,
  labelled,
  onNavigate,
}: {
  item: NavigationItem;
  labelled: boolean;
  onNavigate?: () => void;
}) {
  const button = (
    <ListItemButton
      component={NavLink}
      to={item.to}
      end={item.to === '/'}
      onClick={onNavigate}
      aria-label={labelled ? undefined : item.label}
      sx={(theme) => ({
        color: theme.vars.palette.text.muted,
        justifyContent: labelled ? 'initial' : 'center',
        minHeight: RAIL_ITEM_SIZE,
        mx: labelled ? 1.5 : 'auto',
        px: labelled ? 1.5 : 0,
        flexGrow: labelled ? 1 : 0,
        flexShrink: 0,
        width: labelled ? 'auto' : RAIL_ITEM_WIDTH,
        '&:hover': {
          bgcolor: theme.vars.palette.action.hover,
          color: theme.vars.palette.text.secondary,
        },
        '&.active': {
          bgcolor: theme.vars.palette.action.selected,
          color: theme.vars.palette.action.selectedForeground,
        },
        '&.active .MuiListItemText-primary': {
          fontWeight: theme.typography.fontWeightMedium,
        },
      })}
    >
      <ListItemIcon
        sx={{
          color: 'inherit',
          justifyContent: 'center',
          minWidth: labelled ? 40 : 0,
        }}
      >
        {item.icon}
      </ListItemIcon>
      {labelled && <ListItemText primary={item.label} />}
    </ListItemButton>
  );

  return (
    <ListItem disablePadding sx={{ mb: 0.5 }}>
      {labelled ? button : <Tooltip title={item.label} placement="right">{button}</Tooltip>}
    </ListItem>
  );
}

function NavigationList({
  labelled,
  navigationItems,
  onNavigate,
}: {
  labelled: boolean;
  navigationItems: NavigationItem[];
  onNavigate?: () => void;
}) {
  return (
    <List aria-label="Primary navigation" sx={{ py: `${RAIL_ITEM_INSET}px` }}>
      {navigationItems.map((item) => (
        <DrawerNavItem
          key={item.to}
          item={item}
          labelled={labelled}
          onNavigate={onNavigate}
        />
      ))}
    </List>
  );
}

export function AppDrawer({
  drawerWidth,
  headerHeight = componentTokens.shell.headerHeight,
  mobileDrawerWidth = 264,
  mobileOpen,
  onDrawerClose,
  onDrawerToggle,
}: AppDrawerProps) {
  const theme = useTheme();
  const { railInset } = componentTokens.shell;
  const { canViewAnalytics, canViewDashboard, canViewInbox, canViewSettings } = usePermissions();
  const closeMobileDrawer = onDrawerClose ?? onDrawerToggle;

  const navigationItems: NavigationItem[] = [
    ...(canViewDashboard
      ? [{ to: '/', icon: <DashboardIcon />, label: 'Dashboard' }]
      : []),
    ...(canViewInbox
      ? [{ to: '/inbox', icon: <InboxIcon />, label: 'Inbox' }]
      : []),
    ...(canViewAnalytics
      ? [{ to: '/analytics', icon: <AnalyticsIcon />, label: 'Analytics' }]
      : []),
    ...(canViewSettings
      ? [{ to: '/settings', icon: <SettingsOutlinedIcon />, label: 'Settings' }]
      : []),
  ];

  return (
    <Box
      component="nav"
      aria-label="Application navigation"
      sx={{ flexShrink: { sm: 0 }, width: { sm: drawerWidth + railInset } }}
    >
      <Drawer
        variant="temporary"
        open={mobileOpen}
        onClose={closeMobileDrawer}
        ModalProps={{ keepMounted: true }}
        slotProps={{
          paper: { 'aria-label': 'Mobile navigation', id: 'mobile-navigation' },
          transition: {
            // A kept-mounted, initially hidden paper may reject the focus trap's first attempt.
            onEntered: (node: HTMLElement) => {
              if (!node.contains(node.ownerDocument.activeElement)) node.focus({ preventScroll: true });
            },
          },
        }}
        sx={{
          display: { xs: 'block', sm: 'none' },
          '& .MuiDrawer-paper': {
            bgcolor: 'background.paper',
            boxSizing: 'border-box',
            width: mobileDrawerWidth,
            borderRadius: `0 ${componentTokens.MuiPaper.borderRadius}px ${componentTokens.MuiPaper.borderRadius}px 0`,
            ...theme.effects.overlay(theme),
          },
        }}
      >
        <Box sx={{ alignItems: 'center', display: 'flex', minHeight: headerHeight, px: 2.5 }}>
          <BrandMark />
        </Box>
        <NavigationList
          labelled
          navigationItems={navigationItems}
          onNavigate={closeMobileDrawer}
        />
      </Drawer>

      <Drawer
        variant="permanent"
        open
        slotProps={{ paper: { 'aria-label': 'Desktop navigation' } }}
        sx={{
          display: { xs: 'none', sm: 'block' },
          '& .MuiDrawer-paper': {
            bgcolor: 'background.paper',
            ...theme.effects.overlay(theme),
            border: 0,
            borderRadius: `${componentTokens.MuiPaper.borderRadius}px`,
            bottom: railInset,
            boxSizing: 'border-box',
            height: 'auto',
            left: railInset,
            overflowX: 'hidden',
            top: headerHeight,
            width: drawerWidth,
          },
        }}
      >
        <NavigationList labelled={false} navigationItems={navigationItems} />
      </Drawer>
    </Box>
  );
}
