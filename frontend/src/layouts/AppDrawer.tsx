import AccountCircleOutlinedIcon from '@mui/icons-material/AccountCircleOutlined';
import AccountCircleRoundedIcon from '@mui/icons-material/AccountCircleRounded';
import ForumOutlinedIcon from '@mui/icons-material/ForumOutlined';
import ForumRoundedIcon from '@mui/icons-material/ForumRounded';
import InsertChartOutlinedRoundedIcon from '@mui/icons-material/InsertChartOutlinedRounded';
import InsertChartRoundedIcon from '@mui/icons-material/InsertChartRounded';
import SettingsOutlinedIcon from '@mui/icons-material/SettingsOutlined';
import SettingsRoundedIcon from '@mui/icons-material/SettingsRounded';
import SpaceDashboardOutlinedIcon from '@mui/icons-material/SpaceDashboardOutlined';
import SpaceDashboardRoundedIcon from '@mui/icons-material/SpaceDashboardRounded';
import {
  Box,
  Divider,
  Drawer,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Tooltip,
  useTheme,
} from '@mui/material';
import { useId, useSyncExternalStore, type ReactNode } from 'react';
import { NavLink } from 'react-router-dom';

import { componentTokens, effectTokens } from '@/theme';
import { usePermissions } from '@hooks/usePermissions';
import { bridgeTransportStore } from '@store/transportStore';

import { BrandMark } from './BrandMark';
import { VisuallyHidden } from '../components/ui/VisuallyHidden';

interface AppDrawerProps {
  drawerWidth: number;
  headerHeight?: number;
  mobileDrawerWidth?: number;
  mobileOpen: boolean;
  onDrawerClose?: () => void;
  onDrawerToggle?: () => void;
}

interface NavigationItem {
  activeIcon: ReactNode;
  badgeCount?: number;
  icon: ReactNode;
  label: string;
  to: string;
}

const RAIL_ITEM_SIZE = 44;
const RAIL_ICON_SIZE = 20;

function formatUnreadBadge(count: number): string {
  return count > 99 ? '99+' : String(count);
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
  const badgeId = useId();
  const unreadCount = item.badgeCount ?? 0;
  const badgeText = unreadCount > 0 ? formatUnreadBadge(unreadCount) : null;
  const unreadDescription = `${unreadCount} unread ${unreadCount === 1 ? 'conversation' : 'conversations'}`;
  const button = (
    <ListItemButton
      component={NavLink}
      to={item.to}
      end={item.to === '/'}
      onClick={onNavigate}
      aria-label={item.label}
      aria-describedby={badgeText ? badgeId : undefined}
      sx={(theme) => ({
        color: theme.vars.palette.text.muted,
        borderRadius: `${componentTokens.MuiListItemButton.borderRadius}px`,
        flexGrow: labelled ? 1 : 0,
        flexShrink: 0,
        justifyContent: labelled ? 'flex-start' : 'center',
        minHeight: RAIL_ITEM_SIZE,
        mx: labelled ? 1.5 : 'auto',
        px: labelled ? 1.5 : 0,
        width: labelled ? 'auto' : RAIL_ITEM_SIZE,
        transition: `background-color ${effectTokens.motion.duration.fast} ${effectTokens.motion.easing.standard}`,
        '&:hover, &.Mui-focusVisible': {
          bgcolor: theme.vars.palette.action.hover,
          color: theme.vars.palette.text.primary,
        },
        '&.active, &.active:hover, &.active.Mui-focusVisible': {
          bgcolor: theme.vars.palette.action.selected,
          color: theme.vars.palette.action.selectedForeground,
        },
        '&.active::before': {
          backgroundColor: theme.vars.palette.primary.main,
          borderRadius: '0 3px 3px 0',
          content: '""',
          height: 20,
          insetInlineStart: labelled ? '-12px' : 'calc((44px - 100%) / 2 - 10px)',
          position: 'absolute',
          width: 3,
        },
        '&.active .MuiListItemText-primary': { fontWeight: theme.typography.fontWeightMedium },
        '& [data-active-icon], &.active [data-resting-icon]': { display: 'none' },
        '&.active [data-active-icon]': { display: 'inline-flex' },
        '&:focus-visible': {
          outline: `2px solid ${theme.vars.palette.primary.main}`,
          outlineOffset: -2,
        },
        '@media (prefers-reduced-motion: reduce)': { transition: 'none' },
      })}
    >
      <ListItemIcon
        sx={{
          alignItems: 'center',
          color: 'inherit',
          justifyContent: 'center',
          minWidth: labelled ? 34 : 0,
          '& svg': { fontSize: RAIL_ICON_SIZE },
        }}
      >
        <Box component="span" data-resting-icon sx={{ display: 'inline-flex' }}>{item.icon}</Box>
        <Box component="span" data-active-icon>{item.activeIcon}</Box>
      </ListItemIcon>
      {labelled && <ListItemText primary={item.label} />}
      {badgeText && (
        <Box component="span" aria-hidden="true" data-visual={labelled ? undefined : 'rail-unread-badge'}
          sx={{
            alignItems: 'center', bgcolor: 'primary.main', borderRadius: 999, color: 'primary.contrastText',
            display: 'inline-flex', flexShrink: 0, fontSize: '0.75rem', fontVariantNumeric: 'tabular-nums',
            fontWeight: 600, height: labelled ? 20 : 17, justifyContent: 'center', minWidth: labelled ? 22 : 17,
            px: 0.5, ...(!labelled && { position: 'absolute', right: 2, top: 2 }),
          }}>
          {badgeText}
        </Box>
      )}
    </ListItemButton>
  );
  return (
    <ListItem disablePadding>
      {labelled ? button : (
        <Tooltip describeChild enterDelay={250} title={item.label} placement="right">{button}</Tooltip>
      )}
      {badgeText && <VisuallyHidden id={badgeId}>{unreadDescription}</VisuallyHidden>}
    </ListItem>
  );
}

function RailSections({
  labelled,
  onNavigate,
  primaryItems,
  utilityItems,
}: {
  labelled: boolean;
  onNavigate?: () => void;
  primaryItems: NavigationItem[];
  utilityItems: NavigationItem[];
}) {
  return (
    <Box
      sx={{
        display: 'flex',
        flex: 1,
        flexDirection: 'column',
        justifyContent: 'space-between',
        minHeight: 0,
      }}
    >
      <List
        aria-label="Primary navigation"
        sx={{
          display: 'flex',
          flexDirection: 'column',
          gap: 0.75,
          pb: 1,
          pt: 1.5,
        }}
      >
        {primaryItems.map((item) => (
          <DrawerNavItem
            key={item.to}
            item={item}
            labelled={labelled}
            onNavigate={onNavigate}
          />
        ))}
      </List>

      {utilityItems.length > 0 && (
        <Box sx={{ pb: 1.5, pt: 1 }}>
          {labelled ? (
            <Divider sx={{ mb: 1.25, mx: 2 }} />
          ) : (
            <Box
              aria-hidden="true"
              sx={{
                bgcolor: 'divider',
                height: '1px',
                mb: 1.25,
                mx: 'auto',
                width: 28,
              }}
            />
          )}
          <List
            aria-label="Workspace settings"
            sx={{ display: 'flex', flexDirection: 'column', gap: 0.75, py: 0 }}
          >
            {utilityItems.map((item) => (
              <DrawerNavItem
                key={item.to}
                item={item}
                labelled={labelled}
                onNavigate={onNavigate}
              />
            ))}
          </List>
        </Box>
      )}
    </Box>
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
  const { canViewAccount, canViewAnalytics, canViewDashboard, canViewInbox, canViewSettings } = usePermissions();
  const transport = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
  );
  const closeMobileDrawer = onDrawerClose ?? onDrawerToggle;

  const unreadConversations = transport.conversations.filter(
    (conversation) => conversation.unread_count > 0,
  ).length;

  const primaryItems: NavigationItem[] = [
    ...(canViewDashboard
      ? [{
          to: '/',
          icon: <SpaceDashboardOutlinedIcon />,
          activeIcon: <SpaceDashboardRoundedIcon />,
          label: 'Dashboard',
        }]
      : []),
    ...(canViewInbox
      ? [{
          to: '/inbox',
          icon: <ForumOutlinedIcon />,
          activeIcon: <ForumRoundedIcon />,
          label: 'Inbox',
          badgeCount: unreadConversations,
        }]
      : []),
    ...(canViewAnalytics
      ? [{
          to: '/analytics',
          icon: <InsertChartOutlinedRoundedIcon />,
          activeIcon: <InsertChartRoundedIcon />,
          label: 'Analytics',
        }]
      : []),
  ];

  const utilityItems: NavigationItem[] = [
    ...(canViewSettings
      ? [{
          to: '/settings',
          icon: <SettingsOutlinedIcon />,
          activeIcon: <SettingsRoundedIcon />,
          label: 'Settings',
        }]
      : []),
    ...(canViewAccount
      ? [{
          to: '/account',
          icon: <AccountCircleOutlinedIcon />,
          activeIcon: <AccountCircleRoundedIcon />,
          label: 'Account',
        }]
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
            borderRadius: `0 ${componentTokens.MuiPaper.borderRadius}px ${componentTokens.MuiPaper.borderRadius}px 0`,
            boxSizing: 'border-box',
            display: 'flex',
            flexDirection: 'column',
            width: mobileDrawerWidth,
            ...theme.effects.overlay(theme),
          },
        }}
      >
        {/* The narrow app bar covers the drawer's top, with the status row below its toolbar. */}
        <Box
          sx={{
            alignItems: 'center',
            borderBottom: '1px solid',
            borderColor: 'divider',
            display: 'flex',
            minHeight: headerHeight,
            mb: `${componentTokens.FreshnessStatus.narrowRowHeight}px`,
            px: 2.5,
          }}
        >
          <BrandMark />
        </Box>
        <RailSections
          labelled
          primaryItems={primaryItems}
          utilityItems={utilityItems}
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
            borderRadius: 0,
            borderRight: `1px solid ${theme.vars.palette.divider}`,
            bottom: 0,
            boxShadow: 'none',
            boxSizing: 'border-box',
            display: 'flex',
            flexDirection: 'column',
            height: 'auto',
            left: 0,
            overflowX: 'hidden',
            overflowY: 'auto',
            top: `var(--shell-header-height, ${headerHeight}px)`,
            width: drawerWidth,
          },
        }}
      >
        <RailSections
          labelled={false}
          primaryItems={primaryItems}
          utilityItems={utilityItems}
        />
      </Drawer>
    </Box>
  );
}
