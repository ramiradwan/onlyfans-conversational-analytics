import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Collapse,
  Stack,
  Typography,
} from '@mui/material';
import { useId, useState, useSyncExternalStore } from 'react';

import { DashboardOverview, type OverviewProgress } from '../components/dashboard/DashboardOverview';
import { RecentConversations } from '../components/dashboard/RecentConversations';
import { SetupPrompt } from '../components/SetupPrompt';
import { usePermissions } from '../hooks/usePermissions';
import type { AnalyticsMetric, HistoricalCoverage, ProjectionState } from '../protocol';
import {
  bridgeTransportStore,
  type BridgeTransportState,
} from '../store/transportStore';
import { componentTokens } from '../theme/generated/tokens';
import {
  coverageProgressLabel,
  formatAdditiveMetric,
  humanizeCoverageReason,
  humanizeProjectionReason,
  isConfigurationAligned,
  summarizeMetricEvidence,
  type DataReadiness,
  type MetricEvidence,
} from '../utils/dataReadiness';
import {
  extensionConnection,
  extensionIssue,
  protocolErrorText,
  setupIncomplete,
  type StatusMessage,
} from '../utils/statusCopy';

const NUMBER_FORMAT = new Intl.NumberFormat('en-US');
const LOCAL_DATE_TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
});
const LOCAL_DATE_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });

type CreatorDashboardState = Omit<
  Pick<
    BridgeTransportState,
    | 'agent'
    | 'analytics'
    | 'connection'
    | 'conversations'
    | 'coverage'
    | 'liveFreshness'
    | 'projection'
    | 'protocolError'
    | 'readModelState'
    | 'system'
    | 'viewRevision'
  >,
  'agent' | 'protocolError' | 'system'
> & {
  agent: Pick<
    NonNullable<BridgeTransportState['agent']>,
    | 'applied_config_revision'
    | 'applied_history_settings_revision'
    | 'degraded_reason'
    | 'required_config_revision'
    | 'required_history_settings_revision'
    | 'status'
  > | null;
  protocolError: Pick<NonNullable<BridgeTransportState['protocolError']>, 'code' | 'fatal'> | null;
  system: Pick<NonNullable<BridgeTransportState['system']>, 'readiness'> | null;
};

interface CreatorDashboardStore {
  getState(): Readonly<CreatorDashboardState>;
  subscribe(listener: () => void): () => void;
}

interface CreatorDashboardViewProps {
  store?: CreatorDashboardStore;
}

function getIssue(state: ReturnType<CreatorDashboardStore['getState']>): StatusMessage | null {
  if (state.protocolError !== null) {
    return {
      detail: protocolErrorText(state.protocolError),
      severity: state.protocolError.fatal ? 'error' : 'warning',
      title: 'Connection problem',
    };
  }
  if (state.readModelState === 'resyncing') {
    return {
      detail:
        state.viewRevision === null
          ? 'Your numbers will appear in a moment.'
          : 'Showing your last numbers until the refresh finishes.',
      severity: 'info',
      title: 'Refreshing your numbers',
    };
  }
  if (state.readModelState === 'degraded') {
    return state.viewRevision === null
      ? {
          detail: 'Your numbers will appear once the connection is back.',
          severity: 'warning',
          title: 'Numbers unavailable',
        }
      : {
          detail: 'Showing your last numbers while reconnecting.',
          severity: 'warning',
          title: 'Updates paused',
        };
  }
  if (
    state.viewRevision === null &&
    (state.connection === 'disconnected' || state.connection === 'error')
  ) {
    return {
      detail: 'Your numbers will appear once the connection is back.',
      severity: 'error',
      title: 'Numbers unavailable',
    };
  }
  if (state.system?.readiness === 'unavailable') {
    return {
      detail: humanizeProjectionReason(
        state.projection.reason,
        "Your numbers can't be shown right now.",
      ),
      severity: 'error',
      title: 'Numbers unavailable',
    };
  }
  if (state.viewRevision === null) return null;
  if (state.projection.status === 'unavailable') {
    return {
      detail: "Your numbers can't be shown right now. Messages keep syncing in the meantime.",
      severity: 'error',
      title: 'Numbers unavailable',
    };
  }
  if (state.projection.status === 'pending') {
    return {
      detail: 'Counts appear as soon as they are ready.',
      severity: 'info',
      title: 'Preparing your numbers',
    };
  }
  if (setupIncomplete(state.coverage)) return null;
  if (state.coverage.phase === 'blocked') {
    return {
      detail: humanizeCoverageReason(state.coverage.reason, 'Message history sync stopped.'),
      severity: 'warning',
      title: 'Message history needs attention',
    };
  }
  const connection = extensionConnection(state.agent);
  if (connection !== 'applying_settings') {
    const issue = extensionIssue(connection);
    if (issue !== null) return issue;
  }
  if (state.connection === 'disconnected' || state.connection === 'error') {
    return {
      detail: 'Showing your last numbers while reconnecting.',
      severity: 'warning',
      title: 'Updates paused',
    };
  }
  return null;
}

function formatLocal(date: string | null, format: Intl.DateTimeFormat): string | null {
  if (date === null) return null;
  const timestamp = Date.parse(date);
  return Number.isFinite(timestamp) ? format.format(timestamp) : null;
}

function historyProgress(coverage: HistoricalCoverage): OverviewProgress | null {
  if (coverage.status === 'complete' || coverage.phase === 'not_started') return null;
  if (coverage.phase === 'paused' || coverage.phase === 'blocked') return null;
  const discovered = coverage.discovered_conversations;
  return {
    label: coverageProgressLabel(coverage),
    percent:
      discovered && discovered > 0
        ? Math.min(100, (coverage.complete_conversations / discovered) * 100)
        : null,
  };
}

function directionSplit(
  inbound: AnalyticsMetric | null | undefined,
  outbound: AnalyticsMetric | null | undefined,
  readiness: DataReadiness,
): { received: number; sent: number } | null {
  if (readiness.projection.status !== 'current') return null;
  if (typeof inbound?.value !== 'number' || typeof outbound?.value !== 'number') return null;
  return { received: inbound.value, sent: outbound.value };
}

function NumbersBasis({
  evidence,
  messagesCounted,
  projection,
  syncing,
}: {
  evidence: MetricEvidence;
  messagesCounted: number | null;
  projection: Pick<ProjectionState, 'canonical_revision'>;
  /** Sync progress is already on screen, so the partial basis needs no sentence of its own. */
  syncing: boolean;
}) {
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const updated = formatLocal(evidence.asOf, LOCAL_DATE_TIME_FORMAT);
  const start = formatLocal(evidence.observedStart, LOCAL_DATE_FORMAT);
  const end = formatLocal(evidence.observedEnd, LOCAL_DATE_FORMAT);
  const rows = [
    {
      label: 'Counts include',
      value: evidence.partial ? 'Messages synced so far' : 'Your full message history',
    },
    {
      label: 'Messages counted',
      value: messagesCounted === null ? 'Not available' : NUMBER_FORMAT.format(messagesCounted),
    },
    {
      label: 'Message dates',
      value: start && end ? `${start} – ${end}` : 'No messages yet',
    },
    {
      label: 'Data version',
      value:
        projection.canonical_revision > evidence.revision
          ? `${evidence.revision} (updating to ${projection.canonical_revision})`
          : String(evidence.revision),
    },
  ];

  return (
    <Box>
      <Stack
        direction="row"
        sx={{ alignItems: 'center', columnGap: 1, flexWrap: 'wrap', px: 0.5, rowGap: 0.5 }}
      >
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          {[
            evidence.partial
              ? syncing ? null : 'Counts include messages synced so far.'
              : 'Based on your full message history.',
            updated ? `Updated ${updated}.` : null,
          ]
            .filter(Boolean)
            .join(' ')}
        </Typography>
        <Button
          aria-controls={detailsId}
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          size="small"
        >
          {open ? 'Hide details' : 'Details'}
        </Button>
      </Stack>
      <Collapse in={open} id={detailsId}>
        <Box
          component="dl"
          sx={{
            columnGap: 3,
            display: 'grid',
            gridTemplateColumns: 'auto 1fr',
            m: 0,
            pt: 1,
            px: 0.5,
            rowGap: 0.5,
          }}
        >
          {rows.map((row) => (
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
      </Collapse>
    </Box>
  );
}

export default function CreatorDashboardView({
  store = bridgeTransportStore,
}: CreatorDashboardViewProps) {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const { canViewInbox } = usePermissions();
  const hasSnapshot = state.viewRevision !== null;
  const readiness: DataReadiness = {
    coverage: state.coverage,
    projection: state.projection,
    liveFreshness: state.liveFreshness,
    configurationAligned: isConfigurationAligned(
      state.agent as BridgeTransportState['agent'],
    ),
  };
  const issue = getIssue(state);
  const analytics = state.analytics;
  const metrics = [
    analytics?.total_conversations,
    analytics?.total_messages,
    analytics?.inbound_messages,
    analytics?.outbound_messages,
  ];
  const format = (metric: AnalyticsMetric | null | undefined) =>
    formatAdditiveMetric(metric, readiness, (value) => NUMBER_FORMAT.format(value));
  const showSetup = hasSnapshot && setupIncomplete(state.coverage);
  const hasCounts = metrics.some((metric) => (metric?.value ?? 0) > 0);
  const showNumbers = !showSetup || hasCounts;
  const evidence = summarizeMetricEvidence(metrics);
  const progress = hasSnapshot ? historyProgress(state.coverage) : null;

  return (
    <Box
      sx={{
        flex: 1,
        minHeight: 0,
        overflowY: 'auto',
        pb: 3,
      }}
    >
      <Stack
        spacing={2.5}
        sx={{ maxWidth: componentTokens.shell.dashboardMaxWidth, mx: 'auto', width: '100%' }}
      >
        <Typography component="h1" variant="h4">
          Dashboard
        </Typography>

        {issue !== null && (
          <Alert severity={issue.severity} role="alert">
            <AlertTitle>{issue.title}</AlertTitle>
            {issue.detail}
          </Alert>
        )}

        {showSetup && (
          <SetupPrompt
            extensionConnected={extensionConnection(state.agent) === 'connected'}
            title="Finish setup to see your numbers"
          />
        )}

        {showNumbers && (
          <DashboardOverview
            conversations={format(analytics?.total_conversations)}
            isLoading={!hasSnapshot}
            messages={format(analytics?.total_messages)}
            progress={progress}
            received={format(analytics?.inbound_messages)}
            sent={format(analytics?.outbound_messages)}
            split={directionSplit(analytics?.inbound_messages, analytics?.outbound_messages, readiness)}
          />
        )}

        {showNumbers && hasSnapshot && evidence !== null && (
          <NumbersBasis
            evidence={evidence}
            messagesCounted={analytics?.total_messages?.sample_size ?? null}
            projection={state.projection}
            syncing={progress !== null}
          />
        )}

        {hasSnapshot && !showSetup && (
          <RecentConversations
            conversations={state.conversations}
            showInboxLink={canViewInbox}
          />
        )}

        {!hasSnapshot && (
          <Typography role="status" variant="body2" sx={{ color: 'text.secondary', px: 0.5 }}>
            Loading your numbers…
          </Typography>
        )}
      </Stack>
    </Box>
  );
}
