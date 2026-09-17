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
  display: 'grid',
  gap: theme.spacing(1),
  minHeight: theme.spacing(22),
  padding: theme.spacing(3),
  alignContent: 'center',
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
        <Typography sx={{ color: 'text.secondary' }}>{state.message}</Typography>
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
        <Typography component="h2" variant="h6">
          Updating your analytics
        </Typography>
        <Typography sx={{ color: 'text.secondary' }}>{state.message}</Typography>
      </StateCard>
    );
  }

  if (state.status === 'unavailable') {
    return (
      <StateCard role="status">
        <Typography component="h2" variant="h6">
          Analytics are unavailable
        </Typography>
        <Typography sx={{ color: 'text.secondary' }}>{state.message}</Typography>
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

  // One notice per frame: a refresh or its failure takes the place of the early-estimate warning.
  const notice =
    state.status === 'building'
      ? {
          severity: 'info' as const,
          title: 'Updating your analytics',
          detail: `${state.message} ${
            isBaselineFrame ? 'The results below are early estimates.' : 'Your last results are shown below.'
          }`,
        }
      : state.status === 'error'
        ? {
            severity: 'error' as const,
            title: "Couldn't refresh",
            detail: `${state.message} ${
              isBaselineFrame ? 'The early estimates below are still available.' : 'Your last results are shown below.'
            }`,
          }
        : isBaselineFrame
          ? {
              severity: 'warning' as const,
              title: 'Early estimates',
              detail: `${state.message} Use them to spot trends, not for exact numbers.`,
            }
          : null;

  return (
    <Stack spacing={2} aria-busy={state.isRefreshing}>
      {notice && (
        <Alert severity={notice.severity} role={notice.severity === 'error' ? 'alert' : undefined}>
          <AlertTitle>{notice.title}</AlertTitle>
          {notice.detail}
        </Alert>
      )}
      {state.isRefreshing && <LinearProgress aria-label="Refreshing analytics" />}
      <Content $refreshing={state.isRefreshing}>{children}</Content>
    </Stack>
  );
}
