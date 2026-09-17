import MenuIcon from '@mui/icons-material/Menu';
import {
  AppBar,
  Box,
  Button,
  IconButton,
  Popover,
  Stack,
  Toolbar,
  Typography,
} from '@mui/material';
import { useId, useState, useSyncExternalStore } from 'react';
import { Link as RouterLink } from 'react-router-dom';

import { ThemeToggle } from '@/components/ThemeToggle';
import { StatusChip } from '@/components/ui';
import { componentTokens } from '@/theme';
import {
  coverageProgressLabel,
  humanizeProjectionReason,
  isConfigurationAligned,
  isFullyCurrent,
} from '@/utils/dataReadiness';
import {
  extensionConnection,
  extensionIssue,
  extensionLabel,
  insightsLabel,
  newMessagesLabel,
  protocolErrorText,
  setupIncomplete,
} from '@/utils/statusCopy';
import { bridgeTransportStore, type BridgeTransportState } from '@store/transportStore';

import { BRAND_MARK_SIZE, BrandMark } from './BrandMark';

interface AppAppBarProps {
  headerHeight?: number;
  onDrawerToggle: () => void;
}

type StatusPresentation = {
  color: 'success' | 'warning' | 'error' | 'default';
  detail: string;
  label: string;
};

const { desktopRailWidth, railInset } = componentTokens.shell;
/** Centers the brand mark over the desktop navigation rail. */
const BRAND_INSET = railInset + (desktopRailWidth - BRAND_MARK_SIZE) / 2;

export function getStatusPresentation(
  state: Readonly<BridgeTransportState>,
): StatusPresentation {
  const configurationAligned = isConfigurationAligned(state.agent);
  const readiness = {
    coverage: state.coverage,
    projection: state.projection,
    liveFreshness: state.liveFreshness,
    configurationAligned,
  };
  const extension = extensionConnection(state.agent);
  const extensionProblem = extensionIssue(extension);
  const projectionUnavailable =
    state.system?.readiness === 'unavailable' || state.projection.status === 'unavailable';

  if (state.protocolError !== null) {
    return {
      color: 'error',
      detail: protocolErrorText(state.protocolError),
      label: 'Action needed',
    };
  }

  if (
    state.viewRevision === null &&
    !projectionUnavailable &&
    state.connection !== 'disconnected' &&
    state.connection !== 'error'
  ) {
    return {
      color: 'default',
      detail: 'Loading your latest data.',
      label: 'Connecting',
    };
  }

  if (state.viewRevision !== null && setupIncomplete(state.coverage)) {
    const extensionReady = extension === 'connected';
    return {
      color: 'warning',
      detail: extensionReady
        ? 'Turn on message history to continue setup.'
        : 'Connect the browser extension to continue setup.',
      label: extensionReady ? 'Setup 2 of 3' : 'Setup 1 of 3',
    };
  }

  if (extensionProblem !== null) {
    return extension === 'applying_settings'
      ? {
          color: 'warning',
          detail: extensionProblem.detail,
          label: 'Applying settings',
        }
      : {
          color: 'error',
          detail: extensionProblem.detail,
          label: 'Action needed',
        };
  }

  if (state.viewRevision === null || projectionUnavailable) {
    return {
      color: 'error',
      detail: humanizeProjectionReason(
        state.projection.reason,
        "Your conversations can't be shown right now.",
      ),
      label: 'Data unavailable',
    };
  }

  if (
    state.liveFreshness.status !== 'current' ||
    state.connection === 'disconnected' ||
    state.connection === 'error' ||
    state.connection === 'reconnecting' ||
    state.readModelState === 'degraded'
  ) {
    return {
      color: 'warning',
      detail: 'Your data is shown, but new messages may take longer to appear.',
      label: 'Updates delayed',
    };
  }

  if (state.coverage.status !== 'complete' && state.coverage.phase === 'paused') {
    return {
      color: 'warning',
      detail: 'Message history sync is paused. You can resume it in Settings.',
      label: 'History paused',
    };
  }

  if (state.coverage.status !== 'complete') {
    return {
      color: 'warning',
      detail: `${coverageProgressLabel(state.coverage)}. Numbers grow as older messages arrive.`,
      label: 'Syncing history',
    };
  }

  if (
    isFullyCurrent(readiness) &&
    state.readModelState !== 'resyncing' &&
    state.system?.readiness !== 'degraded'
  ) {
    return {
      color: 'success',
      detail: 'Your message history is synced and your insights are current.',
      label: 'Up to date',
    };
  }

  return {
    color: 'warning',
    detail: 'Your latest messages are being added to your insights.',
    label: 'Updating insights',
  };
}

/** Coverage, projection, and live freshness stay separately visible alongside the combined label. */
export function getStatusRows(
  state: Readonly<BridgeTransportState>,
): readonly { label: string; value: string }[] {
  return [
    { label: 'Browser extension', value: extensionLabel(extensionConnection(state.agent)) },
    { label: 'Message history', value: coverageProgressLabel(state.coverage) },
    { label: 'Insights', value: insightsLabel(state.projection) },
    { label: 'New messages', value: newMessagesLabel(state.liveFreshness) },
  ];
}

export function AppAppBar({
  headerHeight = componentTokens.shell.headerHeight,
  onDrawerToggle,
}: AppAppBarProps) {
  const transportState = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  const status = getStatusPresentation(transportState);
  const [statusAnchor, setStatusAnchor] = useState<HTMLElement | null>(null);
  const statusOpen = statusAnchor !== null;
  const statusDetailsId = useId();

  return (
    <AppBar
      component="header"
      position="fixed"
      color="inherit"
      elevation={0}
      sx={{
        bgcolor: 'background.default',
        color: 'text.primary',
        height: headerHeight,
        justifyContent: 'center',
      }}
    >
      <Toolbar
        disableGutters
        sx={{
          gap: 1.5,
          minHeight: `${headerHeight}px !important`,
          pl: { xs: 1, sm: `${BRAND_INSET}px` },
          pr: { xs: 2, sm: 3, lg: 4 },
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

        <Box sx={{ flex: 1, minWidth: 0 }}>
          <BrandMark />
        </Box>

        <StatusChip
          aria-controls={statusOpen ? statusDetailsId : undefined}
          aria-expanded={statusOpen}
          aria-haspopup="dialog"
          aria-label={`Status: ${status.label}. Show details`}
          aria-live="polite"
          label={status.label}
          onClick={(event) => setStatusAnchor(event.currentTarget)}
          tone={status.color}
        />
        <Popover
          id={statusDetailsId}
          open={statusOpen}
          anchorEl={statusAnchor}
          onClose={() => setStatusAnchor(null)}
          anchorOrigin={{ horizontal: 'right', vertical: 'bottom' }}
          transformOrigin={{ horizontal: 'right', vertical: 'top' }}
          slotProps={{ paper: { 'aria-label': 'Status details', role: 'dialog' } }}
        >
          <Stack spacing={1.5} sx={{ maxWidth: 320, p: 2 }}>
            <Box>
              <Typography variant="subtitle2">{status.label}</Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                {status.detail}
              </Typography>
            </Box>
            <Box
              component="dl"
              sx={{
                columnGap: 2,
                display: 'grid',
                gridTemplateColumns: 'auto 1fr',
                m: 0,
                rowGap: 0.5,
              }}
            >
              {getStatusRows(transportState).map((row) => (
                <Box key={row.label} sx={{ display: 'contents' }}>
                  <Typography component="dt" variant="body2" sx={{ color: 'text.secondary' }}>
                    {row.label}
                  </Typography>
                  <Typography component="dd" variant="body2" sx={{ m: 0 }}>
                    {row.value}
                  </Typography>
                </Box>
              ))}
            </Box>
            <Button
              component={RouterLink}
              onClick={() => setStatusAnchor(null)}
              size="small"
              sx={{ alignSelf: 'flex-start' }}
              to="/settings"
              variant="outlined"
            >
              Open settings
            </Button>
          </Stack>
        </Popover>

        <ThemeToggle />
      </Toolbar>
    </AppBar>
  );
}
