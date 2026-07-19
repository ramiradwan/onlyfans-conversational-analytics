import CloudDoneOutlinedIcon from '@mui/icons-material/CloudDoneOutlined';
import CloudOffOutlinedIcon from '@mui/icons-material/CloudOffOutlined';
import MenuIcon from '@mui/icons-material/Menu';
import SensorsOutlinedIcon from '@mui/icons-material/SensorsOutlined';
import SyncOutlinedIcon from '@mui/icons-material/SyncOutlined';
import WarningAmberOutlinedIcon from '@mui/icons-material/WarningAmberOutlined';
import {
  AppBar,
  Chip,
  IconButton,
  Stack,
  Toolbar,
  Tooltip,
  Typography,
  useTheme,
} from '@mui/material';
import { useSyncExternalStore } from 'react';
import { Link as RouterLink } from 'react-router-dom';

import { ThemeToggle } from '@/components/ThemeToggle';
import {
  coverageProgressLabel,
  isConfigurationAligned,
  isFullyCurrent,
} from '@/utils/dataReadiness';
import { bridgeTransportStore, type BridgeTransportState } from '@store/transportStore';

interface AppAppBarProps {
  drawerWidth: number;
  headerHeight?: number;
  onDrawerToggle: () => void;
}

type StatusPresentation = {
  color: 'success' | 'warning' | 'error' | 'default';
  detail: string;
  icon: React.ReactElement;
  label: string;
};

export function getStatusPresentation(
  state: Readonly<BridgeTransportState>,
): StatusPresentation {
  const dimensions = `Coverage: ${state.coverage.status}. Projection: ${state.projection.status}. Live freshness: ${state.liveFreshness.status}.`;
  const configurationAligned = isConfigurationAligned(state.agent);
  const readiness = {
    coverage: state.coverage,
    projection: state.projection,
    liveFreshness: state.liveFreshness,
    configurationAligned,
  };
  const configurationMismatch = state.agent !== null && !configurationAligned;

  if (state.protocolError !== null || state.agent?.degraded_reason || configurationMismatch) {
    const detail =
      state.protocolError?.detail ??
      state.agent?.degraded_reason ??
      'The requested history configuration has not been applied by the bound Agent.';
    return {
      color: 'error',
      detail: `${detail} ${dimensions}`,
      icon: <WarningAmberOutlinedIcon />,
      label: 'Action needed',
    };
  }

  if (
    state.viewRevision === null ||
    state.system?.readiness === 'unavailable' ||
    state.projection.status === 'unavailable'
  ) {
    return {
      color: 'error',
      detail: `${state.system?.detail ?? 'No valid Brain projection is available.'} ${dimensions}`,
      icon: <CloudOffOutlinedIcon />,
      label: 'Data unavailable',
    };
  }

  if (
    state.liveFreshness.status !== 'current' ||
    state.agent?.status === 'stale' ||
    state.agent?.status === 'disconnected' ||
    state.connection === 'disconnected' ||
    state.connection === 'error' ||
    state.connection === 'reconnecting' ||
    state.readModelState === 'degraded'
  ) {
    return {
      color: 'warning',
      detail: `The last valid projection remains visible, but newer activity may be delayed. ${dimensions}`,
      icon: <CloudOffOutlinedIcon />,
      label: 'Updates delayed',
    };
  }

  if (state.coverage.status !== 'complete' && state.coverage.phase === 'paused') {
    return {
      color: 'warning',
      detail: `${coverageProgressLabel(state.coverage)}. ${dimensions}`,
      icon: <CloudOffOutlinedIcon />,
      label: 'History paused',
    };
  }

  if (state.coverage.status !== 'complete') {
    return {
      color: 'warning',
      detail: `${coverageProgressLabel(state.coverage)}. ${dimensions}`,
      icon: <SyncOutlinedIcon />,
      label: 'Syncing history',
    };
  }

  if (
    state.projection.status !== 'current' ||
    state.projection.projected_revision < state.projection.canonical_revision ||
    state.readModelState === 'resyncing' ||
    state.system?.readiness === 'degraded'
  ) {
    return {
      color: 'warning',
      detail: `Historical acquisition is complete while Brain updates its projections. ${dimensions}`,
      icon: <SyncOutlinedIcon />,
      label: 'Updating insights',
    };
  }

  if (isFullyCurrent(readiness)) {
    return {
      color: 'success',
      detail: `Historical coverage, analytics projection, and live updates are current. ${dimensions}`,
      icon: <CloudDoneOutlinedIcon />,
      label: 'Up to date',
    };
  }

  return {
    color: 'warning',
    detail: `The current state has not met every up-to-date invariant. ${dimensions}`,
    icon: <SensorsOutlinedIcon />,
    label: 'Updating insights',
  };
}

export function AppAppBar({
  drawerWidth,
  headerHeight = 72,
  onDrawerToggle,
}: AppAppBarProps) {
  const theme = useTheme();
  const transportState = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  const status = getStatusPresentation(transportState);

  return (
    <AppBar
      component="header"
      position="fixed"
      elevation={0}
      sx={{
        color: 'text.primary',
        height: headerHeight,
        justifyContent: 'center',
        ml: { sm: `${drawerWidth}px` },
        width: { sm: `calc(100% - ${drawerWidth}px)` },
        ...theme.effects.glassmorphism(theme),
        ...theme.effects.headerBorder(theme),
      }}
    >
      <Toolbar
        disableGutters
        sx={{
          gap: 1.5,
          minHeight: `${headerHeight}px !important`,
          px: { xs: 2, sm: 2.5, lg: 3 },
        }}
      >
        <IconButton
          color="inherit"
          aria-label="Open navigation"
          aria-controls="mobile-navigation"
          edge="start"
          onClick={onDrawerToggle}
          sx={{ display: { sm: 'none' } }}
        >
          <MenuIcon />
        </IconButton>

        <Stack sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="subtitle1" noWrap sx={{ fontWeight: 700, lineHeight: 1.2 }}>
            Bridge
          </Typography>
          <Typography variant="caption" color="text.disabled" noWrap>
            Conversational analytics
          </Typography>
        </Stack>

        <Tooltip title={status.detail}>
          <Chip
            aria-live="polite"
            aria-label={`${status.label}. Open data settings`}
            color={status.color}
            component={RouterLink}
            icon={status.icon}
            label={status.label}
            size="small"
            variant="outlined"
            to="/settings"
            sx={{
              display: { xs: 'none', sm: 'inline-flex' },
              bgcolor: 'background.paper',
              fontWeight: 700,
            }}
          />
        </Tooltip>

        <ThemeToggle />
      </Toolbar>
    </AppBar>
  );
}
