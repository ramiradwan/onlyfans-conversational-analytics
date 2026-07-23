import AnalyticsIcon from '@mui/icons-material/Analytics';
import DashboardIcon from '@mui/icons-material/Dashboard';
import HubOutlinedIcon from '@mui/icons-material/HubOutlined';
import InboxIcon from '@mui/icons-material/Inbox';
import SettingsOutlinedIcon from '@mui/icons-material/SettingsOutlined';
import TravelExploreIcon from '@mui/icons-material/TravelExplore';
import {
  Avatar,
  Box,
  Drawer,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Stack,
  Tooltip,
  Typography,
  useTheme,
} from '@mui/material';
import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';

import { usePermissions } from '@hooks/usePermissions';

interface AppDrawerProps {
  drawerWidth: number;
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

function BrandMark({ labelled }: { labelled: boolean }) {
  return (
    <Stack
      direction="row"
      spacing={1.25}
      sx={{
        alignItems: 'center',
        minHeight: 72,
        px: labelled ? 2 : 0,
        justifyContent: labelled ? 'flex-start' : 'center'
      }}>
      <Box
        aria-hidden="true"
        sx={(theme) => ({
          alignItems: 'center',
          background: `linear-gradient(140deg, ${theme.vars.palette.primary.light}, ${theme.vars.palette.primary.main})`,
          borderRadius: 1.75,
          boxShadow: `0 8px 18px -8px ${theme.vars.palette.primary.main}`,
          color: theme.vars.palette.primary.contrastText,
          display: 'flex',
          flex: '0 0 auto',
          height: 40,
          justifyContent: 'center',
          width: 40,
        })}
      >
        <HubOutlinedIcon fontSize="small" />
      </Box>
      {labelled && (
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="subtitle1" noWrap sx={{ fontWeight: 700, lineHeight: 1.2 }}>
            Bridge
          </Typography>
          <Typography variant="caption" noWrap sx={{
            color: 'text.disabled'
          }}>
            Creator studio
          </Typography>
        </Box>
      )}
    </Stack>
  );
}

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
        borderRadius: labelled ? 1.75 : 1.75,
        color: theme.vars.palette.text.disabled,
        justifyContent: labelled ? 'initial' : 'center',
        minHeight: 46,
        mx: labelled ? 1.25 : 'auto',
        px: labelled ? 1.5 : 0,
        width: labelled ? 'auto' : 46,
        '&:hover': {
          bgcolor: theme.vars.palette.action.hover,
          color: theme.vars.palette.text.secondary,
        },
        '&.active': {
          bgcolor: theme.vars.palette.action.selected,
          color: theme.vars.palette.primary.main,
        },
        '&.active .MuiListItemIcon-root': {
          color: theme.vars.palette.primary.main,
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
    <ListItem disablePadding sx={{ mb: 0.75 }}>
      {labelled ? button : <Tooltip title={item.label} placement="right">{button}</Tooltip>}
    </ListItem>
  );
}

function DrawerContent({
  labelled,
  navigationItems,
  onNavigate,
}: {
  labelled: boolean;
  navigationItems: NavigationItem[];
  onNavigate?: () => void;
}) {
  return (
    <Stack sx={{ height: '100%', minHeight: 0 }}>
      <BrandMark labelled={labelled} />
      <List
        aria-label="Primary navigation"
        sx={{ flex: '0 0 auto', px: labelled ? 0.75 : 0, py: 1.5 }}
      >
        {navigationItems.map((item) => (
          <DrawerNavItem
            key={item.to}
            item={item}
            labelled={labelled}
            onNavigate={onNavigate}
          />
        ))}
      </List>
      <Box sx={{ mt: 'auto', p: labelled ? 2 : 1.75 }}>
        <Tooltip title={labelled ? '' : 'Account'} placement="right">
          <Stack
            direction="row"
            spacing={1.25}
            sx={{
              alignItems: 'center',
              justifyContent: labelled ? 'flex-start' : 'center'
            }}>
            <Avatar
              aria-label="Account"
              sx={(theme) => ({
                bgcolor: theme.vars.palette.action.selected,
                color: theme.vars.palette.primary.main,
                fontSize: theme.typography.caption.fontSize,
                fontWeight: theme.typography.fontWeightBold,
                height: 38,
                width: 38,
              })}
            >
              B
            </Avatar>
            {labelled && (
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="body2" noWrap sx={{
                  fontWeight: 700
                }}>
                  Bridge account
                </Typography>
                <Typography variant="caption" noWrap sx={{
                  color: 'text.disabled'
                }}>
                  Workspace
                </Typography>
              </Box>
            )}
          </Stack>
        </Tooltip>
      </Box>
    </Stack>
  );
}

export function AppDrawer({
  drawerWidth,
  mobileDrawerWidth = 264,
  mobileOpen,
  onDrawerClose,
  onDrawerToggle,
}: AppDrawerProps) {
  const theme = useTheme();
  const { canViewAnalytics, canViewDashboard, canViewGraphExplorer, canViewInbox, canViewSettings } =
    usePermissions();
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
    ...(canViewGraphExplorer
      ? [
          {
            to: '/graph-explorer',
            icon: <TravelExploreIcon />,
            label: 'Graph Explorer',
          },
        ]
      : []),
    ...(canViewSettings
      ? [{ to: '/settings', icon: <SettingsOutlinedIcon />, label: 'Settings' }]
      : []),
  ];

  return (
    <Box
      component="nav"
      aria-label="Application navigation"
      sx={{ flexShrink: { sm: 0 }, width: { sm: drawerWidth } }}
    >
      <Drawer
        variant="temporary"
        open={mobileOpen}
        onClose={closeMobileDrawer}
        ModalProps={{ keepMounted: true }}
        slotProps={{ paper: { 'aria-label': 'Mobile navigation', id: 'mobile-navigation' } }}
        sx={{
          display: { xs: 'block', sm: 'none' },
          '& .MuiDrawer-paper': {
            bgcolor: 'background.paper',
            boxSizing: 'border-box',
            width: mobileDrawerWidth,
            ...theme.effects.sideBorder(theme),
          },
        }}
      >
        <DrawerContent
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
            border: 0,
            boxSizing: 'border-box',
            overflowX: 'hidden',
            width: drawerWidth,
            ...theme.effects.sideBorder(theme),
          },
        }}
      >
        <DrawerContent labelled={false} navigationItems={navigationItems} />
      </Drawer>
    </Box>
  );
}
