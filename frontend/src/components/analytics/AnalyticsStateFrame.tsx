import ErrorOutlineRoundedIcon from '@mui/icons-material/ErrorOutlineRounded';
import {
  Alert,
  AlertTitle,
  Box,
  Button,
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
  alignContent: 'center',
  alignSelf: 'center',
  display: 'grid',
  gap: theme.spacing(1),
  marginInline: 'auto',
  maxWidth: 640,
  minHeight: theme.spacing(22),
  padding: theme.spacing(3),
  width: '100%',
  ...theme.effects.cardBorder(theme),
}));

const StateIcon = styled(Box)(({ theme }) => ({
  alignItems: 'center',
  backgroundColor: `color-mix(in oklch, ${theme.vars.palette.error.main} 10%, ${theme.vars.palette.background.paper})`,
  borderRadius: `${theme.shape.borderRadius}px`,
  color: theme.vars.palette.error.main,
  display: 'flex',
  height: 40,
  justifyContent: 'center',
  marginBottom: theme.spacing(0.5),
  width: 40,
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

const LoadingPanels = styled(Box)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(2),
  gridTemplateColumns: 'minmax(0, 1fr)',
  [theme.breakpoints.up('md')]: {
    gridTemplateColumns: 'minmax(0, 2fr) minmax(17rem, 1fr)',
  },
}));

const primaryPanelHeight = componentTokens.analytics.chartHeight + 96;

export interface AnalyticsStateFrameProps {
  state: AnalyticsReadState;
  children?: ReactNode;
  onRetry?: () => void;
}

export function AnalyticsStateFrame({ state, children, onRetry }: AnalyticsStateFrameProps) {
  if (state.status === 'loading') {
    return (
      <Stack spacing={2} role="status" aria-live="polite">
        <Typography sx={{ color: 'text.secondary' }}>{state.message}</Typography>
        <LoadingPanels>
          <Skeleton
            data-visual="analytics-loading-primary"
            animation={false}
            height={primaryPanelHeight}
            variant="rounded"
          />
          <Skeleton
            data-visual="analytics-loading-replies"
            animation={false}
            height={primaryPanelHeight}
            variant="rounded"
          />
        </LoadingPanels>
        <Skeleton
          data-visual="analytics-loading-topics"
          animation={false}
          height={220}
          variant="rounded"
        />
      </Stack>
    );
  }

  if (state.status === 'building' && state.data === null) {
    return (
      <StateCard data-visual="analytics-empty-state" role="status">
        <Typography component="h2" variant="h6">
          Updating your analytics
        </Typography>
        <Typography sx={{ color: 'text.secondary' }}>{state.message}</Typography>
      </StateCard>
    );
  }

  if (state.status === 'unavailable') {
    return (
      <StateCard data-visual="analytics-empty-state" role="status">
        <Typography component="h2" variant="h6">
          Analytics are unavailable
        </Typography>
        <Typography sx={{ color: 'text.secondary' }}>{state.message}</Typography>
      </StateCard>
    );
  }

  if (state.status === 'error' && state.data === null) {
    return (
      <StateCard data-visual="analytics-empty-state" role="alert">
        <StateIcon aria-hidden="true">
          <ErrorOutlineRoundedIcon fontSize="small" />
        </StateIcon>
        <Typography component="h2" variant="h6">
          Analytics couldn&apos;t load
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          {state.message}
        </Typography>
        {onRetry && (
          <Button onClick={onRetry} size="small" sx={{ justifySelf: 'start', mt: 1 }} variant="contained">
            Try again
          </Button>
        )}
      </StateCard>
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
