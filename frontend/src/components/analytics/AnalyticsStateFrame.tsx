import BuildCircleOutlinedIcon from '@mui/icons-material/BuildCircleOutlined';
import {
  Alert,
  AlertTitle,
  Box,
  LinearProgress,
  Paper,
  Skeleton,
  Stack,
  Typography,
  styled,
} from '@mui/material';
import type { ReactNode } from 'react';

import type { AnalyticsReadState } from '../../analytics';
import { componentTokens } from '../../theme';

const StateCard = styled(Paper)(({ theme }) => ({
  alignItems: 'center',
  display: 'flex',
  flexDirection: 'column',
  gap: theme.spacing(1),
  justifyContent: 'center',
  minHeight: theme.spacing(30),
  padding: theme.spacing(4),
  textAlign: 'center',
  ...theme.effects.cardBorder(theme),
}));

const Content = styled(Box, {
  shouldForwardProp: (property) => property !== '$refreshing',
})<{ $refreshing: boolean }>(({ theme, $refreshing }) => ({
  opacity: $refreshing ? componentTokens.analytics.refreshOpacity : 1,
  position: 'relative',
  transition: `opacity ${theme.transitions.duration.shorter}ms ${theme.transitions.easing.easeInOut}`,
  '@media (prefers-reduced-motion: reduce)': {
    transition: 'none',
  },
}));

const LoadingGrid = styled(Box)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(2),
  gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
  [theme.breakpoints.up('md')]: {
    gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
  },
}));

export interface AnalyticsStateFrameProps {
  state: AnalyticsReadState;
  children?: ReactNode;
}

export function AnalyticsStateFrame({ state, children }: AnalyticsStateFrameProps) {
  if (state.status === 'loading') {
    return (
      <Stack spacing={2} role="status" aria-live="polite">
        <Typography sx={{
          color: 'text.secondary'
        }}>{state.message}</Typography>
        <LoadingGrid>
          {Array.from({ length: 4 }, (_, index) => (
            <Skeleton key={index} variant="rounded" height={112} animation={false} />
          ))}
        </LoadingGrid>
        <Skeleton variant="rounded" height={300} animation={false} />
      </Stack>
    );
  }

  if (state.status === 'building' && state.data === null) {
    return (
      <StateCard role="status">
        <BuildCircleOutlinedIcon color="disabled" fontSize="large" />
        <Typography component="h2" variant="h6">
          Analytics are building
        </Typography>
        <Typography sx={{
          color: 'text.secondary'
        }}>{state.message}</Typography>
      </StateCard>
    );
  }

  if (state.status === 'unavailable') {
    return (
      <StateCard role="status">
        <BuildCircleOutlinedIcon color="disabled" fontSize="large" />
        <Typography component="h2" variant="h6">
          Analytics are unavailable
        </Typography>
        <Typography sx={{
          color: 'text.secondary'
        }}>{state.message}</Typography>
      </StateCard>
    );
  }

  if (state.status === 'error' && state.data === null) {
    return (
      <Alert severity="error" role="alert">
        <AlertTitle>Analytics could not be loaded</AlertTitle>
        {state.message}
      </Alert>
    );
  }

  const isBaselineFrame =
    state.status === 'baseline' ||
    ((state.status === 'error' || state.status === 'building') &&
      state.previousStatus === 'baseline');

  return (
    <Stack spacing={2} aria-busy={state.isRefreshing}>
      {state.status === 'building' && (
        <Alert severity="info">
          <AlertTitle>Fresh analytics are building</AlertTitle>
          {state.message} The last complete frame remains below.
        </Alert>
      )}
      {isBaselineFrame && (
        <Alert severity="warning">
          <AlertTitle>Directional baseline</AlertTitle>
          {state.status === 'baseline'
            ? state.message
            : 'The retained frame is a directional baseline — not calibrated production analysis.'}{' '}
          Use these signals for exploration, not calibrated decisions.
        </Alert>
      )}
      {state.status === 'error' && (
        <Alert severity="error" role="alert">
          <AlertTitle>Refresh failed</AlertTitle>
          {state.message} The last complete frame remains below.
        </Alert>
      )}
      {state.isRefreshing && <LinearProgress aria-label="Refreshing analytics" />}
      <Content $refreshing={state.isRefreshing}>{children}</Content>
    </Stack>
  );
}
