import {
  Box,
  Button,
  Popover,
  Stack,
  Typography,
} from '@mui/material';
import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react';
import { Link as RouterLink } from 'react-router-dom';

import { BrowserExtensionControls } from '../components/BrowserExtensionControls';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { DashboardOverview, type OverviewProgress } from '../components/dashboard/DashboardOverview';
import { RecentConversations } from '../components/dashboard/RecentConversations';
import { MessageHistoryControls } from '../components/MessageHistoryControls';
import { DashboardConnection, OnboardingEmptyActivity } from '../components/OnboardingEmptyActivity';
import { SetupPrompt } from '../components/SetupPrompt';
import { OnboardingAction, OnboardingCard, OnboardingSpot, OnboardingStatus } from '../components/ui/OnboardingCard';
import { usePermissions } from '../hooks/usePermissions';
import type { AnalyticsMetric, HistoricalCoverage } from '../protocol';
import { browserControlApi, type BrowserControlApi } from '../services/browserControlApi';
import {
  capabilityLicenseApi,
  type CapabilityLicenseApi,
} from '../services/capabilityLicenseApi';
import { desktopOnboardingApi, selectAnalysisReadiness, selectCollectionContinuation, selectFullPresentationIntent, type DesktopOnboardingApi } from '../services/desktopOnboarding';
import type { HistorySettingsApi } from '../services/historySettingsApi';
import {
  bridgeTransportStore,
  type BridgeTransportState,
  type BridgeTransportStore,
} from '../store/transportStore';
import { componentTokens, effectTokens } from '../theme/generated/tokens';
import { createConnectionGrace, type ConnectionPhase } from '../utils/connectionGrace';
import {
  coverageProgressLabel,
  coverageProgressPercent,
  formatAdditiveMetric,
  humanizeCoverageReason,
  humanizeProjectionReason,
  isConfigurationAligned,
  isFullyCurrent,
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
const LOCAL_TIME_FORMAT = new Intl.DateTimeFormat(undefined, { timeStyle: 'short' });
const LOCAL_DATE_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });
const DETAILS_BUTTON_SX = {
  justifyContent: 'flex-start',
  px: 0.5,
  '&:focus-visible': { outlineOffset: '-' + effectTokens.focus.width },
} as const;
const ISSUE_TONE = { info: 'pending', warning: 'attention', error: 'error' } as const;


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
  onboardingApi?: DesktopOnboardingApi;
  browserApi?: BrowserControlApi;
  historyApi?: HistorySettingsApi;
  historyStore?: BridgeTransportStore;
}

type DashboardIssue = StatusMessage & { updatesStopped?: boolean };

function getIssue(
  state: ReturnType<CreatorDashboardStore['getState']>,
  hasDisplayedNumbers: boolean,
  connectionPhase: ConnectionPhase,
  softwareUpdateRequired: boolean,
): DashboardIssue | null {
  if (softwareUpdateRequired) return {
    title: 'Update Conversation Analytics to continue.', detail: '', severity: 'warning',
  };
  if (state.protocolError !== null) {
    if (state.protocolError.code === 'unsupported_version') return {
      title: 'This page is out of date.', detail: '', severity: 'error', updatesStopped: state.protocolError.fatal,
    };
    if (!state.protocolError.fatal && hasDisplayedNumbers && state.readModelState !== 'resyncing') return {
      title: 'Couldn’t check the latest update. Showing your saved counts.', detail: '', severity: 'warning',
      updatesStopped: state.readModelState === 'degraded',
    };
    return {
      detail: protocolErrorText(state.protocolError),
      severity: state.protocolError.fatal ? 'error' : 'warning',
      title: 'Connection problem',
      updatesStopped: state.protocolError.fatal,
    };
  }
  if (state.viewRevision !== null && connectionPhase === 'interrupted') {
    if (state.connection === 'reconnecting' || state.connection === 'handshaking') return {
      title: 'Reconnecting to the app…', detail: '', severity: 'info',
    };
    if (state.connection === 'disconnected' || state.connection === 'error') return {
      title: hasDisplayedNumbers ? 'Dipsy can’t connect right now. Showing your saved counts.' : 'Dashboard connection interrupted',
      detail: '', severity: hasDisplayedNumbers ? 'warning' : 'error', updatesStopped: true,
    };
  }
  if (state.readModelState === 'resyncing') {
    return {
      detail: hasDisplayedNumbers ? 'Showing your last numbers.' : '',
      severity: 'info',
      title: 'Refreshing your numbers',
    };
  }
  if (state.readModelState === 'degraded' && state.connection === 'connected') {
    return {
      detail: '', severity: 'warning', updatesStopped: true,
      title: hasDisplayedNumbers ? 'Couldn’t check the latest update. Showing your saved counts.'
        : state.viewRevision === null ? 'Numbers unavailable' : 'Updates unavailable',
    };
  }
  if (
    state.viewRevision === null &&
    (state.connection === 'disconnected' || state.connection === 'error')
  ) {
    return {
      detail: '',
      severity: 'error',
      title: 'Dashboard connection interrupted',
    };
  }
  if (state.system?.readiness === 'unavailable') {
    return {
      detail: humanizeProjectionReason(
        state.projection.reason,
        '',
      ),
      severity: 'error',
      title: 'Numbers unavailable',
    };
  }
  if (state.viewRevision === null) return null;
  if (state.projection.status === 'unavailable') {
    return {
      detail: '',
      severity: 'error',
      title: 'Numbers unavailable',
    };
  }
  if (state.projection.status === 'pending') {
    return {
      detail: '',
      severity: 'info',
      title: 'Waiting for numbers',
    };
  }
  if (setupIncomplete(state.coverage)) return null;
  if (state.coverage.phase === 'blocked') {
    return {
      detail: humanizeCoverageReason(state.coverage.reason, ''),
      severity: 'warning',
      title: 'History incomplete',
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
  return { label: coverageProgressLabel(coverage), percent: coverageProgressPercent(coverage) };
}

function directionSplit(
  inbound: AnalyticsMetric | null | undefined,
  outbound: AnalyticsMetric | null | undefined,
  readiness: DataReadiness,
): { received: number; sent: number; sample: { count: number; asOf: string } | null } | null {
  if (readiness.projection.status !== 'current') return null;
  if (typeof inbound?.value !== 'number' || typeof outbound?.value !== 'number'
    || inbound.projection_revision !== readiness.projection.projected_revision
    || outbound.projection_revision !== readiness.projection.projected_revision) return null;
  const count = inbound.value + outbound.value;
  const sameSample = Number.isSafeInteger(count) && count === inbound.sample_size && count === outbound.sample_size
    && inbound.as_of === outbound.as_of && inbound.basis === outbound.basis
    && inbound.observed_range.start === outbound.observed_range.start
    && inbound.observed_range.end === outbound.observed_range.end
    && inbound.complete_range?.start === outbound.complete_range?.start
    && inbound.complete_range?.end === outbound.complete_range?.end;
  return { received: inbound.value, sent: outbound.value, sample: sameSample ? { count, asOf: inbound.as_of } : null };
}

function NumbersBasis({
  evidence,
  messagesCounted,
  sample,
  updatesStopped,
}: {
  evidence: MetricEvidence;
  messagesCounted: number | null;
  sample: NonNullable<ReturnType<typeof directionSplit>>['sample'];
  updatesStopped: boolean;
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const open = anchor !== null;
  const detailsId = useId();
  const titleId = useId();
  const updated = updatesStopped ? formatLocal(sample?.asOf ?? null, LOCAL_TIME_FORMAT) : null;
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
        sx={{ alignItems: 'flex-start', rowGap: 0.5 }}
      >
        <Button
          aria-controls={open ? detailsId : undefined}
          aria-expanded={open}
          aria-haspopup="dialog"
          onClick={(event) => setAnchor(event.currentTarget)}
          size="small"
          sx={DETAILS_BUTTON_SX}
        >
          Details
        </Button>
        {sample && sample.count > 0 && <Typography data-dashboard-saved-basis variant="body2" sx={{ color: 'text.secondary' }}>
          {`Based on ${NUMBER_FORMAT.format(sample.count)} messages saved on this computer so far.${updated ? ` Last updated ${updated}.` : ''}`}
        </Typography>}
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

function PausedCollection({ api, canReview, current }: {
  api: BrowserControlApi;
  canReview: boolean;
  current: () => boolean;
}) {
  const operation = useRef<AbortController | null>(null);
  const [opening, setOpening] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  useEffect(() => () => operation.current?.abort(), []);

  async function review() {
    if (!canReview || !current() || operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setOpening(true);
    setFeedback(null);
    try {
      const result = await api.open('setup', controller.signal);
      if (!controller.signal.aborted && result === 'unreachable') setFeedback('Chrome didn’t open. Open Chrome to continue.');
    } catch {
      if (!controller.signal.aborted) setFeedback('The change was not confirmed.');
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setOpening(false);
    }
  }

  return <Box data-journey-state="desktop.message_collection_review" sx={{ maxWidth: 760 }}>
    <OnboardingCard title="Paused by you" body="New messages aren’t being collected." spot={<OnboardingSpot icon="pause" />}
      actions={canReview && <OnboardingAction><Button aria-disabled={opening || undefined} aria-busy={opening || undefined}
        onClick={() => void review()} variant="contained">Review in Chrome</Button></OnboardingAction>}
      status={feedback && <OnboardingStatus tone="attention">{feedback}</OnboardingStatus>} />
  </Box>;
}

export default function CreatorDashboardView({
  store = bridgeTransportStore,
  activationApi = capabilityLicenseApi,
  onboardingApi = desktopOnboardingApi,
  browserApi = browserControlApi,
  historyApi,
  historyStore = bridgeTransportStore,
}: CreatorDashboardViewProps) {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const { canViewInbox, canManageHistorySync } = usePermissions();
  const desktop = useSyncExternalStore(onboardingApi.subscribe, onboardingApi.getState, onboardingApi.getState);
  const analysis = selectAnalysisReadiness(desktop, state.creatorAccountId);
  const fullAnalyticsReady = analysis.checking ? undefined : analysis.value === null ? null
    : analysis.value.commercial_authority === 'active' && analysis.value.analysis_admission === 'admitted';
  const hasSnapshot = state.viewRevision !== null;
  const [grace] = useState(createConnectionGrace);
  const connectionPhase = useSyncExternalStore(grace.subscribe, grace.getSnapshot);
  const interrupted = hasSnapshot && (state.connection === 'disconnected' || state.connection === 'error'
    || state.connection === 'reconnecting' || state.connection === 'handshaking');
  useEffect(() => () => grace.dispose(), [grace, state.creatorAccountId]);
  useEffect(() => { grace.observe(!interrupted, interrupted); }, [grace, interrupted, state.creatorAccountId]);

  const readiness: DataReadiness = {
    coverage: state.coverage,
    projection: state.projection,
    liveFreshness: state.liveFreshness,
    configurationAligned: isConfigurationAligned(
      state.agent as BridgeTransportState['agent'],
    ),
  };
  const analytics = state.analytics;
  const metrics = [
    analytics?.total_conversations,
    analytics?.total_messages,
    analytics?.inbound_messages,
    analytics?.outbound_messages,
  ];
  const format = (metric: AnalyticsMetric | null | undefined) =>
    formatAdditiveMetric(metric, readiness, (value) => Number(value) > 9_999_999 ? '10M+' : NUMBER_FORMAT.format(value)).replace(/\+\+$/, '+');
  const agent = state.agent as BridgeTransportState['agent'];
  const connection = extensionConnection(agent);
  const browser = connection === 'connected' && state.connection === 'connected'
    && agent?.creator_account_id === state.creatorAccountId ? agent?.browser ?? null : null;
  const collectionOn = browser?.capture === 'active' && browser.site_access === 'granted' && !browser.legal_review_required;
  // The current browser owner can admit collection before commercial activation.
  // A reported off state is not Full intent or consent; review remains explicit.
  const collection = selectCollectionContinuation(desktop, state.creatorAccountId, browser, canManageHistorySync);
  const activationInProgress = analysis.availability === 'current' && Boolean(
    desktop.pending && (desktop.pendingAction?.startsWith('activation.') || desktop.pendingAction?.startsWith('auth.'))
    || desktop.snapshot?.pending_operations.some(operation => operation.action.startsWith('activation.')
      || operation.action.startsWith('auth.'))
      || desktop.snapshot?.decision?.action === 'activate-complimentary' && analysis.value?.commercial_authority !== 'active'
      || desktop.snapshot?.decision_status?.state === 'accepted' && analysis.value?.commercial_authority !== 'active');
  const canShowCollection = collection.current && !activationInProgress && state.creatorAccountId && browser
    && (collection.reviewNeeded || collection.paused) && (collection.actionable || fullAnalyticsReady === true)
    ? state.creatorAccountId : null;
  const collectionReview = canShowCollection ? collection.paused
    ? <PausedCollection key={canShowCollection} api={browserApi} canReview={collection.canResume}
      current={() => store.getState() === state && onboardingApi.getState() === desktop} />
    : <BrowserExtensionControls key={canShowCollection} browser={browser} api={browserApi}
      onboarding={onboardingApi} connection={connection} canManage={canManageHistorySync}
      continuationCreatorId={canShowCollection} /> : null;
  const fullIntent = canManageHistorySync ? selectFullPresentationIntent(desktop, state.creatorAccountId, browser) : undefined;
  const activationAdmitted = analysis.availability === 'current' && desktop.snapshot?.local_ready
    && desktop.snapshot.allowed_actions.includes('activation.review');
  const showCommercial = fullAnalyticsReady !== true && (connection === 'connected' || activationAdmitted || activationInProgress);
  const showSetup = !collectionReview && hasSnapshot && state.creatorAccountId && fullAnalyticsReady !== undefined
    && (connection !== 'connected' || fullAnalyticsReady !== true);

  const historyScope = fullAnalyticsReady === true && analysis.availability === 'current'
    && canManageHistorySync && state.creatorAccountId && collectionOn
    && desktop.snapshot?.journey_id && desktop.snapshot.generation
    ? JSON.stringify([state.creatorAccountId, desktop.snapshot.journey_id]) : null;
  const visibleMetrics = metrics.filter(metric => metric?.value !== null && metric?.value !== undefined
    && metric.projection_revision === readiness.projection.projected_revision
    && readiness.projection.status === 'current');
  const hasActivity = state.conversations.length > 0 || visibleMetrics.some(metric => typeof metric?.value === 'number' && metric.value > 0);
  const historyEligible = historyScope !== null && !collectionReview && !hasActivity
    && state.coverage.status !== 'complete';
  const historyPrompt = historyEligible && state.creatorAccountId
    ? <MessageHistoryControls key={historyScope} reviewCreatorId={state.creatorAccountId} store={historyStore}
      api={historyApi} browserApi={browserApi} desktopApi={onboardingApi} continuation /> : null;

  const evidence = summarizeMetricEvidence(visibleMetrics);
  const split = directionSplit(analytics?.inbound_messages, analytics?.outbound_messages, readiness);
  const progress = hasSnapshot ? historyProgress(state.coverage) : null;
  const allZero = !hasActivity && visibleMetrics.length === metrics.length && visibleMetrics.every(metric => metric?.value === 0);
  const issue = getIssue(state, hasSnapshot && !allZero && visibleMetrics.length > 0,
    connectionPhase, browser?.history_error_code === 'software_update_required');
  const currentEmpty = hasSnapshot && !hasActivity && issue === null && collectionOn
    && fullAnalyticsReady === true && analysis.availability === 'current' && state.readModelState === 'realtime';
  const completeEmpty = currentEmpty && allZero && isFullyCurrent(readiness) && split?.sample?.count === 0
    && metrics.every(metric => metric?.basis === 'complete' && metric.sample_size === 0 && metric.as_of === split.sample?.asOf);
  const waitingForMessages = currentEmpty && setupIncomplete(state.coverage)
    && (allZero || state.projection.canonical_revision === 0);
  const connectionSummary = <DashboardConnection browser={browser} connection={connection}
    desktopConnected={state.connection === 'connected'} />;
  const continuation = collectionReview ? <Stack spacing={0.5}>
    {collectionReview}
    {activationAdmitted && fullAnalyticsReady !== true && !desktop.pending && !collection.reviewOperation && <Button component={RouterLink} to="/settings#full-analytics"
      sx={{ alignSelf: 'flex-start' }}>Full analytics</Button>}
  </Stack> : historyPrompt ?? (showSetup
    ? showCommercial
      ? <CommercialActivationControls api={activationApi} onboarding={onboardingApi} creatorAccountId={state.creatorAccountId} presentationIntent={fullIntent} />
      : <SetupPrompt title="Set up Chrome to see your message counts" compact />
    : completeEmpty || waitingForMessages ? <OnboardingEmptyActivity waitingForMessages={waitingForMessages} collectionOn={collectionOn} />
      : hasSnapshot && fullAnalyticsReady === true ? connectionSummary : null);
  const hideEmptyOverview = completeEmpty || waitingForMessages || !hasActivity && (collectionReview !== null || Boolean(showSetup && !showCommercial));
  const overviewLoading = !hasSnapshot || allZero;
  const opening = !state.creatorAccountId && !hasSnapshot && issue === null;
  const updatesStopped = Boolean(collection.current && collection.paused || issue?.updatesStopped);

  return (
    <Box data-scroll-container sx={{ flex: 1, minHeight: 0, overflowY: 'auto', scrollbarGutter: 'stable', pb: 3 }}>
      <Stack sx={{ maxWidth: componentTokens.shell.dashboardMaxWidth, mx: 'auto', width: '100%',
        '& > [data-reserved-region]:not(:empty)': { mt: 2.5 } }}>
        <Typography component="h1" variant="h4">Dashboard</Typography>
        <Box data-reserved-region="dashboard-overview">
          {!opening && !hideEmptyOverview && <Box data-region-content>
            <DashboardOverview key={state.creatorAccountId} isLoading={overviewLoading} conversations={format(analytics?.total_conversations)}
              messages={format(analytics?.total_messages)} progress={progress} received={format(analytics?.inbound_messages)}
              sent={format(analytics?.outbound_messages)} split={split} />
          </Box>}
        </Box>
        <Box data-reserved-region="dashboard-basis">
          {!overviewLoading && !hideEmptyOverview && evidence !== null && <Box data-region-content>
            <NumbersBasis evidence={evidence} messagesCounted={visibleMetrics.includes(analytics?.total_messages)
              ? analytics?.total_messages?.sample_size ?? null : null} sample={split?.sample ?? null} updatesStopped={updatesStopped} />
          </Box>}
        </Box>
        <Box data-reserved-region="dashboard-notice"
          sx={{ '& .onboarding-status > svg': { inlineSize: 20, blockSize: 20, flexShrink: 0 } }}>
          {issue && <Box data-region-content role="alert">
            <OnboardingStatus tone={ISSUE_TONE[issue.severity]} live={false}>
              {[issue.title, issue.detail].filter(Boolean).join('. ')}
            </OnboardingStatus>
          </Box>}
        </Box>
        <Box data-reserved-region="dashboard-recent">
          {hasSnapshot && state.conversations.length > 0 && <Box data-region-content>
            <RecentConversations conversations={state.conversations} showInboxLink={canViewInbox} />
          </Box>}
        </Box>
        <Box data-reserved-region="dashboard-setup">
          {opening ? <Box data-region-content sx={{ maxWidth: 760 }}><OnboardingCard title="Opening Dipsy" spot={<OnboardingSpot icon="app" />}
            status={<OnboardingStatus tone="pending">Checking your connection.</OnboardingStatus>} /></Box>
            : continuation && <Box data-region-content>{continuation}</Box>}
        </Box>
      </Stack>
    </Box>
  );
}
