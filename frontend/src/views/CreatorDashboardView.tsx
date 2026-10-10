import {
  Box,
  Button,
  Popover,
  Stack,
  Typography,
} from '@mui/material';
import { useEffect, useId, useState, useSyncExternalStore } from 'react';

import { DashboardOverview, type OverviewProgress } from '../components/dashboard/DashboardOverview';
import { RecentConversations } from '../components/dashboard/RecentConversations';
import { OnboardingEmptyActivity } from '../components/OnboardingEmptyActivity';
import { SetupPrompt } from '../components/SetupPrompt';
import { LoadingFrame, ReservedNotice, ReservedRegion, StatusLine } from '../components/ui/ReservedRegion';
import { usePermissions } from '../hooks/usePermissions';
import type { AnalyticsMetric, HistoricalCoverage } from '../protocol';
import {
  capabilityLicenseApi,
  type CapabilityLicenseApi,
} from '../services/capabilityLicenseApi';
import { journeyFromHash } from '../services/onboardingSession';
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
const READINESS_WAIT_MS = 3000;


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
  creatorAccountId?: string | null;
  session?: { connection_id: string } | null;
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
  activationApi?: CapabilityLicenseApi;
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
  if (state.readModelState === 'degraded' && state.connection === 'connected') {
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
  return null;
}

function formatLocal(date: string | null, format: Intl.DateTimeFormat): string | null {
  if (date === null) return null;
  const timestamp = Date.parse(date);
  return Number.isFinite(timestamp) ? format.format(timestamp) : null;
}

function historyProgress(coverage: HistoricalCoverage): OverviewProgress | null {
  if (coverage.status === 'complete') return { label: coverageProgressLabel(coverage), percent: 100, complete: true };
  if (coverage.phase === 'not_started') return null;
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
  syncing,
}: {
  evidence: MetricEvidence;
  messagesCounted: number | null;
  /** Sync progress is already on screen, so the partial basis needs no sentence of its own. */
  syncing: boolean;
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const open = anchor !== null;
  const detailsId = useId();
  const titleId = useId();
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
  ];

  return (
    <Box>
      <Stack
        sx={{ alignItems: 'flex-start', px: 0.5, rowGap: 0.5 }}
      >
        <Typography variant="body2" sx={{ color: 'text.secondary', height: { xs: '3.75rem', sm: '2.5rem' } }}>
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
          aria-controls={open ? detailsId : undefined}
          aria-expanded={open}
          aria-haspopup="dialog"
          onClick={(event) => setAnchor(event.currentTarget)}
          size="small"
        >
          Details
        </Button>
      </Stack>
      <Popover
        anchorEl={anchor}
        anchorOrigin={{ horizontal: 'left', vertical: 'bottom' }}
        id={detailsId}
        onClose={() => setAnchor(null)}
        open={open}
        slotProps={{
          paper: {
            'aria-labelledby': titleId,
            role: 'dialog',
            sx: { maxWidth: 360, p: 2 },
          },
        }}
        transformOrigin={{ horizontal: 'left', vertical: 'top' }}
      >
        <Typography component="h2" id={titleId} variant="subtitle2" sx={{ mb: 1 }}>
          How these numbers are counted
        </Typography>
        <Box
          component="dl"
          sx={{
            columnGap: 3,
            display: 'grid',
            gridTemplateColumns: 'auto 1fr',
            m: 0,
            rowGap: 0.75,
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
      </Popover>
    </Box>
  );
}

export default function CreatorDashboardView({
  store = bridgeTransportStore,
  activationApi = capabilityLicenseApi,
}: CreatorDashboardViewProps) {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const { canViewInbox } = usePermissions();
  const scope = String(state.creatorAccountId) + ':' + String(state.session?.connection_id);
  const [setup, setSetup] = useState<{ scope: string; value: boolean | null | undefined }>({ scope, value: undefined });
  const fullAnalyticsReady = setup.scope === scope ? setup.value : undefined;
  const hasSnapshot = state.viewRevision !== null;

  useEffect(() => {
    const controller = new AbortController();
    let pending = true;
    const settle = (next: boolean | null) => {
      if (!pending) return;
      pending = false;
      setSetup({ scope, value: next });
    };
    const timeout = window.setTimeout(() => {
      settle(null);
      controller.abort();
    }, READINESS_WAIT_MS);
    void activationApi.readiness(controller.signal).then(
      (next) => settle(next.commercial_authority === 'active' && next.analysis_admission === 'admitted'),
      () => settle(null),
    ).finally(() => window.clearTimeout(timeout));
    return () => {
      pending = false;
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [activationApi, scope]);

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
    formatAdditiveMetric(metric, readiness, (value) => Number(value) > 9_999_999 ? '10M+' : NUMBER_FORMAT.format(value)).replace(/\+\+$/, '+');
  const showSetup = hasSnapshot && fullAnalyticsReady !== undefined
    && (extensionConnection(state.agent) !== 'connected' || fullAnalyticsReady === false);

  const evidence = summarizeMetricEvidence(metrics);
  const progress = hasSnapshot ? historyProgress(state.coverage) : null;
  const emptyOnboarding = Boolean(journeyFromHash(window.location.hash)) && hasSnapshot
    && state.conversations.length === 0 && issue === null && fullAnalyticsReady === true
    && state.connection === 'connected' && metrics.every((metric) => metric?.value === 0 && metric.basis === 'complete');
  if (emptyOnboarding) return (
    <Box data-scroll-container sx={{ flex: 1, minHeight: 0, overflowY: 'auto', pb: 3 }}>
      <Stack spacing={3} sx={{ maxWidth: componentTokens.shell.dashboardMaxWidth, mx: 'auto', width: '100%' }}>
        <Typography component="h1" variant="h4">Dashboard</Typography>
        <OnboardingEmptyActivity browser={(state.agent as BridgeTransportState['agent'])?.browser ?? null}
          connection={extensionConnection(state.agent)} desktopConnected={state.connection === 'connected'} />
      </Stack>
    </Box>
  );

  return (
    <Box data-scroll-container sx={{ flex: 1, minHeight: 0, overflowY: 'auto', scrollbarGutter: 'stable', pb: 3 }}>
      <Stack spacing={2.5} sx={{ maxWidth: componentTokens.shell.dashboardMaxWidth, mx: 'auto', width: '100%' }}>
        <Typography component="h1" variant="h4">Dashboard</Typography>
        <ReservedNotice id="dashboard-notice" notice={issue ? { title: issue.title, body: issue.detail, severity: issue.severity } : fullAnalyticsReady === null ? { title: 'Setup status is unavailable.', body: '', severity: 'warning' } : null} />
        <StatusLine id="dashboard-setup-status" text={fullAnalyticsReady === undefined ? 'Checking setup…' : null} />
        <ReservedRegion id="dashboard-overview" size={{ xs: 450, sm: 300 }}>
          <Box data-region-content><DashboardOverview isLoading={!hasSnapshot} conversations={format(analytics?.total_conversations)} messages={format(analytics?.total_messages)} progress={progress} received={format(analytics?.inbound_messages)} sent={format(analytics?.outbound_messages)} split={directionSplit(analytics?.inbound_messages, analytics?.outbound_messages, readiness)} /></Box>
          {!hasSnapshot && <Box sx={{ position: 'absolute', inset: 0 }}><LoadingFrame label="Loading dashboard…" /></Box>}
        </ReservedRegion>
        <ReservedRegion id="dashboard-basis" size={{ xs: 128, sm: 88 }}>
          {hasSnapshot && evidence !== null && <Box data-region-content><NumbersBasis evidence={evidence} messagesCounted={analytics?.total_messages?.sample_size ?? null} syncing={progress !== null && !progress.complete} /></Box>}
        </ReservedRegion>
        <ReservedRegion id="dashboard-recent" size={{ xs: 440, sm: 360 }} regionRole="scroll">
          {hasSnapshot ? <Box data-region-content><RecentConversations conversations={state.conversations} showInboxLink={canViewInbox} /></Box> : <LoadingFrame label="Loading dashboard…" />}
        </ReservedRegion>
        <ReservedRegion id="dashboard-setup" size={{ xs: 720, sm: 600 }} regionRole="scroll">
          {showSetup && <Box data-region-content>
            <SetupPrompt extensionConnected={extensionConnection(state.agent) === 'connected'} fullAnalyticsReady={fullAnalyticsReady === true} title="Finish setup" />
          </Box>}
        </ReservedRegion>
      </Stack>
    </Box>
  );
}
