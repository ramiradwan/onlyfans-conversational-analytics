import DataObjectOutlinedIcon from '@mui/icons-material/DataObjectOutlined';
import {
  Alert,
  AlertTitle,
  Box,
  Chip,
  Grid,
  LinearProgress,
  Stack,
  Typography,
} from '@mui/material';
import { useSyncExternalStore } from 'react';

import { KpiCard } from '../components/KpiCard';
import { Panel } from '../components/ui';
import { bridgeTransportStore } from '../store/transportStore';
import {
  coverageProgressLabel,
  formatAdditiveMetric,
  isConfigurationAligned,
  isFullyCurrent,
  metricEvidenceLabel,
} from '../utils/dataReadiness';

const NUMBER_FORMAT = new Intl.NumberFormat('en-US');

export default function AnalyticsView() {
  const state = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  const readiness = {
    coverage: state.coverage,
    projection: state.projection,
    liveFreshness: state.liveFreshness,
    configurationAligned: isConfigurationAligned(state.agent),
  };
  const hasSnapshot = state.viewRevision !== null;
  const kpis = [
    ['Total conversations', state.analytics?.total_conversations],
    ['Total messages', state.analytics?.total_messages],
    ['Inbound messages', state.analytics?.inbound_messages],
    ['Outbound messages', state.analytics?.outbound_messages],
  ] as const;
  const progress = state.snapshotProgress.percentage;

  return (
    <Box sx={{ maxWidth: 1280, mx: 'auto', width: '100%', pb: 3 }}>
      <Stack spacing={3}>
        <Stack
          alignItems={{ sm: 'flex-end' }}
          direction={{ xs: 'column', sm: 'row' }}
          justifyContent="space-between"
          spacing={2}
        >
          <Box>
            <Typography component="h1" variant="h4">Analytics</Typography>
            <Typography color="text.secondary" variant="body2" sx={{ mt: 0.5 }}>
              Counts are labelled as lower bounds until historical coverage is proven complete.
            </Typography>
            <Typography color="text.secondary" display="block" sx={{ mt: 0.5 }} variant="caption">
              Coverage: {state.coverage.status} · As of{' '}
              {state.coverage.as_of
                ? new Date(state.coverage.as_of).toLocaleString()
                : 'not established'}
            </Typography>
          </Box>
          <Chip
            color={isFullyCurrent(readiness) ? 'success' : 'warning'}
            label={isFullyCurrent(readiness) ? 'Up to date' : coverageProgressLabel(state.coverage)}
            variant="outlined"
          />
        </Stack>

        {state.projection.status === 'unavailable' ? (
          <Alert severity="error">
            <AlertTitle>Analytics unavailable</AlertTitle>
            The projection is unavailable. Captured data remains local and can be reprojected.
          </Alert>
        ) : state.projection.status !== 'current' ? (
          <Alert severity="warning">
            <AlertTitle>Projection not current</AlertTitle>
            Analytics are waiting for a consistent projection; unknown values are shown as —.
          </Alert>
        ) : state.coverage.status !== 'complete' ? (
          <Alert severity="info">
            <AlertTitle>{coverageProgressLabel(state.coverage)}</AlertTitle>
            Counts marked + are lower bounds. Every displayed subset metric identifies its range,
            sample size, and evidence basis; no value implies lifetime completeness.
          </Alert>
        ) : state.liveFreshness.status !== 'current' ? (
          <Alert severity="warning">
            <AlertTitle>Live activity is delayed</AlertTitle>
            Historical analytics are complete, but the newest platform activity may not be included.
          </Alert>
        ) : null}

        {progress !== null && state.coverage.status !== 'complete' && (
          <Panel>
            <Stack spacing={1}>
              <Stack direction="row" justifyContent="space-between">
                <Typography fontWeight={700} variant="body2">Historical coverage</Typography>
                <Typography color="text.secondary" variant="body2">{progress}%</Typography>
              </Stack>
              <LinearProgress value={progress} variant="determinate" />
            </Stack>
          </Panel>
        )}

        <Grid container spacing={{ xs: 2, md: 3 }} aria-busy={!hasSnapshot}>
          {kpis.map(([title, value]) => (
            <Grid key={title} size={{ xs: 12, sm: 6, lg: 3 }}>
              <KpiCard
                grow
                isLoading={!hasSnapshot}
                title={title}
                detail={metricEvidenceLabel(value)}
                value={formatAdditiveMetric(value, readiness, (number) =>
                  NUMBER_FORMAT.format(number),
                )}
              />
            </Grid>
          ))}

          <Grid size={{ xs: 12, md: 4 }} sx={{ display: 'flex' }}>
            <Panel sx={{ flex: 1 }}>
              <Typography component="h2" variant="h6">Sentiment</Typography>
              <UnavailableDetail ready={state.projection.status === 'current'} label="sentiment distribution" />
            </Panel>
          </Grid>
          <Grid size={{ xs: 12, md: 4 }} sx={{ display: 'flex' }}>
            <Panel sx={{ flex: 1 }}>
              <Typography component="h2" variant="h6">Topics</Typography>
              <UnavailableDetail ready={state.projection.status === 'current'} label="topic metrics" />
            </Panel>
          </Grid>
          <Grid size={{ xs: 12, md: 4 }} sx={{ display: 'flex' }}>
            <Panel sx={{ flex: 1 }}>
              <Typography component="h2" variant="h6">Trends</Typography>
              <UnavailableDetail ready={state.projection.status === 'current'} label="time-series metrics" />
            </Panel>
          </Grid>
        </Grid>
      </Stack>
    </Box>
  );
}

function UnavailableDetail({ ready, label }: { ready: boolean; label: string }) {
  return (
    <Stack alignItems="center" justifyContent="center" spacing={1} sx={{ minHeight: 180, textAlign: 'center' }}>
      <DataObjectOutlinedIcon color="disabled" sx={{ fontSize: 36 }} />
      <Typography color="text.secondary" variant="body2">
        {ready
          ? `The current bounded snapshot does not expose ${label}.`
          : `${label[0].toUpperCase()}${label.slice(1)} are unavailable until the projection is current.`}
      </Typography>
    </Stack>
  );
}
