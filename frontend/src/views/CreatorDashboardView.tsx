import CloudDoneIcon from '@mui/icons-material/CloudDone';
import CloudOffIcon from '@mui/icons-material/CloudOff';
import DataObjectIcon from '@mui/icons-material/DataObject';
import SyncIcon from '@mui/icons-material/Sync';
import {
  Alert,
  AlertTitle,
  Box,
  Chip,
  CircularProgress,
  Divider,
  Grid,
  Skeleton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material';
import { LineChart } from '@mui/x-charts/LineChart';
import { PieChart } from '@mui/x-charts/PieChart';
import { useMemo, useSyncExternalStore } from 'react';

import {
  buildCreatorDashboardModel,
  type MessageActivityPoint,
  type Sentiment,
} from './creatorDashboardModel';
import { KpiCard } from '../components/KpiCard';
import { Panel } from '../components/ui';
import {
  bridgeTransportStore,
  type BridgeTransportState,
} from '../store/transportStore';
import { componentTokens } from '../theme/generated/tokens';
import {
  canShowCompleteAnalytics,
  coverageProgressLabel,
  formatAdditiveMetric,
  humanizeCoverageReason,
  isConfigurationAligned,
  isFullyCurrent,
  metricEvidenceLabel,
} from '../utils/dataReadiness';

const CHART_HEIGHT = 284;
const NUMBER_FORMAT = new Intl.NumberFormat('en-US');
const UTC_DATE_FORMAT = new Intl.DateTimeFormat('en-US', {
  day: 'numeric',
  month: 'short',
  timeZone: 'UTC',
});
const UTC_DATE_TIME_FORMAT = new Intl.DateTimeFormat('en-US', {
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  month: 'short',
  timeZone: 'UTC',
  timeZoneName: 'short',
  year: 'numeric',
});

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
  protocolError: Pick<NonNullable<BridgeTransportState['protocolError']>, 'detail' | 'fatal'> | null;
  system: Pick<NonNullable<BridgeTransportState['system']>, 'detail' | 'readiness'> | null;
};

interface CreatorDashboardStore {
  getState(): Readonly<CreatorDashboardState>;
  subscribe(listener: () => void): () => void;
}

interface CreatorDashboardViewProps {
  store?: CreatorDashboardStore;
}

interface IssuePresentation {
  detail: string;
  severity: 'error' | 'warning';
  title: string;
}

function getIssue(state: ReturnType<CreatorDashboardStore['getState']>): IssuePresentation | null {
  if (state.protocolError !== null) {
    return {
      detail: state.protocolError.detail,
      severity: state.protocolError.fatal ? 'error' : 'warning',
      title: 'Bridge communication error',
    };
  }
  if (state.readModelState === 'resyncing') {
    return {
      detail:
        state.viewRevision === null
          ? 'Refreshing snapshot. Waiting for a complete analytics snapshot.'
          : 'Refreshing snapshot. Showing the latest complete snapshot while a fresh snapshot is requested.',
      severity: 'warning',
      title: 'Refreshing analytics',
    };
  }
  if (state.readModelState === 'degraded') {
    return {
      detail:
        state.viewRevision === null
          ? state.system?.detail ?? 'Updates paused. Analytics are unavailable until the Bridge reconnects.'
          : state.system?.detail ?? 'Updates paused. Showing cached analytics until the Bridge reconnects.',
      severity: 'warning',
      title: state.viewRevision === null ? 'Analytics unavailable' : 'Realtime updates paused',
    };
  }
  if (
    state.viewRevision === null &&
    (state.connection === 'disconnected' || state.connection === 'error')
  ) {
    return {
      detail: 'Analytics are unavailable until the Bridge reconnects.',
      severity: 'error',
      title: 'Analytics unavailable',
    };
  }
  if (state.system?.readiness === 'unavailable') {
    return {
      detail: state.system.detail ?? 'The processing service is currently unavailable.',
      severity: 'error',
      title: 'Analytics unavailable',
    };
  }
  if (state.viewRevision === null) return null;
  if (state.projection.status === 'unavailable') {
    return {
      detail: 'The analytics projection is unavailable. Conversation capture can continue while it recovers.',
      severity: 'error',
      title: 'Analytics unavailable',
    };
  }
  if (state.viewRevision !== null && state.projection.status === 'pending') {
    return {
      detail: 'Brain is building a consistent projection. Counts remain unavailable until it activates.',
      severity: 'warning',
      title: 'Analytics processing',
    };
  }
  if (state.coverage.phase === 'blocked') {
    return {
      detail: humanizeCoverageReason(state.coverage.reason, 'Historical sync is blocked.'),
      severity: 'warning',
      title: 'Historical coverage needs attention',
    };
  }
  if (state.system?.readiness === 'degraded') {
    return {
      detail: state.system.detail ?? 'Analytics processing is operating in a degraded state.',
      severity: 'warning',
      title: 'Analytics processing degraded',
    };
  }
  if (state.agent?.degraded_reason) {
    return {
      detail: state.agent.degraded_reason,
      severity: 'warning',
      title: 'Agent needs attention',
    };
  }
  if (state.agent?.status === 'stale' || state.agent?.status === 'disconnected') {
    return {
      detail: 'New platform activity may be delayed until the Agent reconnects.',
      severity: 'warning',
      title: state.agent.status === 'stale' ? 'Agent connection is stale' : 'Agent disconnected',
    };
  }
  if (
    state.viewRevision !== null &&
    (state.connection === 'disconnected' || state.connection === 'error')
  ) {
    return {
      detail: 'Updates paused. Showing the latest complete snapshot while the Bridge reconnects.',
      severity: 'warning',
      title: 'Realtime updates paused',
    };
  }
  return null;
}

function formatUtcDay(date: string): string {
  if (date === 'Unknown date') return date;
  return UTC_DATE_FORMAT.format(new Date(date + 'T00:00:00.000Z'));
}

function formatUtcDateTime(date: string | null): string {
  if (date === null) return 'No messages';
  const timestamp = Date.parse(date);
  return Number.isFinite(timestamp) ? UTC_DATE_TIME_FORMAT.format(timestamp) : 'Unknown time';
}

function sentimentColor(sentiment: Sentiment, chart: {
  negative: string;
  neutral: string;
  positive: string;
  unknown: string;
}): string {
  return chart[sentiment];
}

function ChartSkeleton() {
  return (
    <Stack spacing={1.5} sx={{ height: CHART_HEIGHT, justifyContent: 'flex-end' }}>
      <Skeleton height="60%" variant="rounded" animation="wave" />
      <Skeleton width="100%" animation="wave" />
    </Stack>
  );
}

function EmptyChart({ children }: { children: string }) {
  return (
    <Stack
      alignItems="center"
      justifyContent="center"
      sx={{ color: 'text.secondary', minHeight: CHART_HEIGHT, textAlign: 'center' }}
    >
      <DataObjectIcon aria-hidden="true" sx={{ fontSize: 36, mb: 1, opacity: 0.55 }} />
      <Typography variant="body2">{children}</Typography>
    </Stack>
  );
}

function ActivityTable({ activity }: { activity: readonly MessageActivityPoint[] }) {
  return (
    <TableContainer sx={{ maxHeight: 240 }}>
      <Table size="small" stickyHeader aria-label="Message activity data in UTC">
        <TableHead>
          <TableRow>
            <TableCell>Date (UTC)</TableCell>
            <TableCell align="right">Inbound</TableCell>
            <TableCell align="right">Outbound</TableCell>
            <TableCell align="right">Total</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {activity.map((point) => (
            <TableRow key={point.date}>
              <TableCell component="th" scope="row">
                {formatUtcDay(point.date)}
              </TableCell>
              <TableCell align="right">{NUMBER_FORMAT.format(point.inbound)}</TableCell>
              <TableCell align="right">{NUMBER_FORMAT.format(point.outbound)}</TableCell>
              <TableCell align="right">{NUMBER_FORMAT.format(point.total)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

export default function CreatorDashboardView({
  store = bridgeTransportStore,
}: CreatorDashboardViewProps) {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const model = useMemo(
    () => buildCreatorDashboardModel(state.analytics, state.conversations),
    [state.analytics, state.conversations],
  );
  const hasSnapshot = state.viewRevision !== null;
  const readiness = {
    coverage: state.coverage,
    projection: state.projection,
    liveFreshness: state.liveFreshness,
    configurationAligned: isConfigurationAligned(
      state.agent as BridgeTransportState['agent'],
    ),
  };
  const completeAnalytics = canShowCompleteAnalytics(readiness);
  const detailedAnalyticsAvailable = false;
  const issue = getIssue(state);
  const isResyncing = state.readModelState === 'resyncing';
  const statusLabel = issue
    ? isResyncing
      ? 'Resyncing'
      : hasSnapshot
        ? 'Cached'
        : 'Unavailable'
    : hasSnapshot && isFullyCurrent(readiness)
      ? 'Up to date'
      : hasSnapshot
        ? coverageProgressLabel(state.coverage)
      : 'Connecting';
  const statusIcon = issue ? (
    isResyncing ? (
      <SyncIcon />
    ) : (
      <CloudOffIcon />
    )
  ) : hasSnapshot && isFullyCurrent(readiness) ? (
    <CloudDoneIcon />
  ) : hasSnapshot ? (
    <SyncIcon />
  ) : (
    <SyncIcon />
  );
  const kpis = [
    { title: 'Total conversations', value: model.analytics?.total_conversations },
    { title: 'Total messages', value: model.analytics?.total_messages },
    { title: 'Inbound messages', value: model.analytics?.inbound_messages },
    { title: 'Outbound messages', value: model.analytics?.outbound_messages },
  ];
  const sentimentTotal = model.sentimentCounts.reduce((total, item) => total + item.count, 0);

  return (
    <Box
      sx={{
        bgcolor: 'background.default',
        flex: 1,
        minHeight: 0,
        overflowY: 'auto',
        pb: 3,
      }}
    >
      <Stack
        spacing={3}
        sx={{ maxWidth: componentTokens.shell.dashboardMaxWidth, mx: 'auto', width: '100%' }}
      >
        <Stack
          alignItems={{ sm: 'flex-end' }}
          direction={{ xs: 'column', sm: 'row' }}
          justifyContent="space-between"
          spacing={2}
        >
          <Box>
            <Typography component="h1" variant="h4">
              Creator dashboard
            </Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }} variant="body2">
              Bounded conversation analytics with explicit coverage and freshness.
            </Typography>
            <Typography color="text.secondary" display="block" sx={{ mt: 0.5 }} variant="caption">
              Coverage: {state.coverage.status} · As of{' '}
              {state.coverage.as_of ? formatUtcDateTime(state.coverage.as_of) : 'not established'} ·
              Projection revision {state.projection.projected_revision}/
              {state.projection.canonical_revision}
            </Typography>
          </Box>
          <Chip
            aria-live="polite"
            color={
              issue
                ? issue.severity === 'error'
                  ? 'error'
                  : 'warning'
                : isFullyCurrent(readiness)
                  ? 'success'
                  : 'warning'
            }
            icon={statusIcon}
            label={statusLabel}
            variant="outlined"
          />
        </Stack>

        {issue !== null && (
          <Alert severity={issue.severity} role="alert">
            <AlertTitle>{issue.title}</AlertTitle>
            {issue.detail}
          </Alert>
        )}

        <Grid container spacing={{ xs: 2, md: 3 }} aria-busy={!hasSnapshot}>
          {kpis.map((kpi) => (
            <Grid key={kpi.title} size={{ xs: 12, sm: 6, lg: 3 }}>
              <KpiCard
                grow
                isLoading={!hasSnapshot}
                title={kpi.title}
                detail={metricEvidenceLabel(kpi.value)}
                value={formatAdditiveMetric(kpi.value, readiness, (value) =>
                  NUMBER_FORMAT.format(value),
                )}
              />
            </Grid>
          ))}

          <Grid size={{ xs: 12, lg: 8 }} sx={{ display: 'flex' }}>
            <Panel sx={{ flex: 1, minWidth: 0 }}>
              <Box>
                <Typography component="h2" id="message-activity-title" variant="h6">
                  Message activity
                </Typography>
                <Typography color="text.secondary" variant="body2">
                  Daily inbound and outbound messages in UTC
                </Typography>
              </Box>

              {!hasSnapshot ? (
                <ChartSkeleton />
              ) : !detailedAnalyticsAvailable ? (
                <EmptyChart>
                  {completeAnalytics
                    ? 'Daily series are not included in the bounded Bridge snapshot.'
                    : 'Trends become available only after historical coverage and projection are complete.'}
                </EmptyChart>
              ) : model.messageActivity.length === 0 ? (
                <EmptyChart>No message activity is available for the complete range.</EmptyChart>
              ) : (
                <>
                  <Box sx={{ minWidth: 0 }}>
                    <LineChart
                      aria-label="Daily inbound and outbound message activity in UTC"
                      axisHighlight={{ x: 'line' }}
                      dataset={model.messageActivity}
                      grid={{ horizontal: true }}
                      height={CHART_HEIGHT}
                      margin={{ bottom: 34, left: 46, right: 18, top: 18 }}
                      series={[
                        {
                          color: 'var(--bridge-palette-chart-categorical1)',
                          curve: 'linear',
                          dataKey: 'inbound',
                          label: 'Inbound',
                          showMark: model.messageActivity.length <= 14,
                          valueFormatter: (value) =>
                            value === null ? 'No value' : NUMBER_FORMAT.format(value),
                        },
                        {
                          color: 'var(--bridge-palette-chart-categorical2)',
                          curve: 'linear',
                          dataKey: 'outbound',
                          label: 'Outbound',
                          showMark: model.messageActivity.length <= 14,
                          valueFormatter: (value) =>
                            value === null ? 'No value' : NUMBER_FORMAT.format(value),
                        },
                      ]}
                      slotProps={{ tooltip: { trigger: 'axis' } }}
                      sx={{
                        '& .MuiLineElement-root': { strokeWidth: 2 },
                        '& .MuiMarkElement-root': {
                          stroke: 'var(--bridge-palette-background-paper)',
                          strokeWidth: 2,
                        },
                      }}
                      xAxis={[
                        {
                          dataKey: 'date',
                          scaleType: 'point',
                          valueFormatter: formatUtcDay,
                        },
                      ]}
                      yAxis={[{ min: 0, valueFormatter: (value: number) => NUMBER_FORMAT.format(value) }]}
                    />
                  </Box>
                  <Typography
                    color="text.secondary"
                    id="message-activity-description"
                    sx={{ mb: 0.5 }}
                    variant="caption"
                  >
                    Hover or focus the chart for daily values. The complete values are listed below.
                  </Typography>
                  <ActivityTable activity={model.messageActivity} />
                </>
              )}
            </Panel>
          </Grid>

          <Grid size={{ xs: 12, lg: 4 }} sx={{ display: 'flex' }}>
            <Panel sx={{ flex: 1, minWidth: 0 }}>
              <Box>
                <Typography component="h2" id="sentiment-mix-title" variant="h6">
                  Sentiment mix
                </Typography>
                <Typography color="text.secondary" variant="body2">
                  All message sentiment classifications
                </Typography>
              </Box>

              {!hasSnapshot ? (
                <ChartSkeleton />
              ) : !detailedAnalyticsAvailable ? (
                <EmptyChart>
                  Sentiment requires complete historical coverage and a projected sentiment series.
                </EmptyChart>
              ) : sentimentTotal === 0 ? (
                <EmptyChart>No sentiment classifications are available for the complete range.</EmptyChart>
              ) : (
                <>
                  <Box sx={{ minWidth: 0 }}>
                    <PieChart
                      aria-label="Message sentiment distribution"
                      height={220}
                      hideLegend
                      series={[
                        {
                          cornerRadius: 4,
                          data: model.sentimentCounts.map((item) => ({
                            color: sentimentColor(item.sentiment, {
                              negative: 'var(--bridge-palette-chart-negative)',
                              neutral: 'var(--bridge-palette-chart-neutral)',
                              positive: 'var(--bridge-palette-chart-positive)',
                              unknown: 'var(--bridge-palette-chart-unknown)',
                            }),
                            id: item.sentiment,
                            label: item.label,
                            value: item.count,
                          })),
                          faded: { additionalRadius: -3, color: 'gray' },
                          highlightScope: { fade: 'global', highlight: 'item' },
                          highlighted: { additionalRadius: 4 },
                          innerRadius: '58%',
                          paddingAngle: 2,
                          sortingValues: 'none',
                          valueFormatter: ({ value }) => NUMBER_FORMAT.format(value),
                        },
                      ]}
                      slotProps={{ tooltip: { trigger: 'item' } }}
                    />
                  </Box>
                  <Table size="small" aria-label="Sentiment mix counts">
                    <TableBody>
                      {model.sentimentCounts.map((item) => (
                        <TableRow key={item.sentiment}>
                          <TableCell component="th" scope="row">
                            <Stack alignItems="center" direction="row" spacing={1}>
                              <Box
                                aria-hidden="true"
                                sx={(theme) => ({
                                  bgcolor: sentimentColor(item.sentiment, {
                                    negative: theme.vars.palette.chart.negative,
                                    neutral: theme.vars.palette.chart.neutral,
                                    positive: theme.vars.palette.chart.positive,
                                    unknown: theme.vars.palette.chart.unknown,
                                  }),
                                  borderRadius: 0.75,
                                  flexShrink: 0,
                                  height: 10,
                                  width: 10,
                                })}
                              />
                              <span>{item.label}</span>
                            </Stack>
                          </TableCell>
                          <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                            {NUMBER_FORMAT.format(item.count)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </>
              )}
            </Panel>
          </Grid>

          <Grid size={{ xs: 12, lg: 7 }} sx={{ display: 'flex' }}>
            <Panel sx={{ flex: 1 }}>
              <Box>
                <Typography component="h2" variant="h6">
                  Most active conversations
                </Typography>
                <Typography color="text.secondary" variant="body2">
                  Ranked by message count, then latest activity and conversation ID
                </Typography>
              </Box>

              {!hasSnapshot ? (
                <Stack aria-label="Loading active conversations" spacing={1.5}>
                  {[1, 2, 3, 4, 5].map((row) => (
                    <Skeleton height={48} key={row} variant="rounded" />
                  ))}
                </Stack>
              ) : !detailedAnalyticsAvailable ? (
                <EmptyChart>
                  Conversation ranking requires complete per-conversation message aggregates.
                </EmptyChart>
              ) : model.mostActiveConversations.length === 0 ? (
                <EmptyChart>No conversations are available for the complete range.</EmptyChart>
              ) : (
                <TableContainer>
                  <Table size="small" aria-label="Most active conversations">
                    <TableHead>
                      <TableRow>
                        <TableCell>#</TableCell>
                        <TableCell>Conversation</TableCell>
                        <TableCell align="right">Messages</TableCell>
                        <TableCell>Latest activity</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {model.mostActiveConversations.map((conversation, index) => (
                        <TableRow key={conversation.conversationId} hover>
                          <TableCell sx={{ color: 'text.secondary' }}>{index + 1}</TableCell>
                          <TableCell component="th" scope="row">
                            <Typography variant="body2" fontWeight={700}>
                              {conversation.displayName}
                            </Typography>
                            <Typography color="text.secondary" variant="caption">
                              {conversation.platformUserId}
                            </Typography>
                          </TableCell>
                          <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                            <Typography component="span" variant="body2" fontWeight={700}>
                              {NUMBER_FORMAT.format(conversation.messageCount)}
                            </Typography>
                            <Typography color="text.secondary" display="block" variant="caption">
                              {NUMBER_FORMAT.format(conversation.inboundCount)} in ·{' '}
                              {NUMBER_FORMAT.format(conversation.outboundCount)} out
                            </Typography>
                          </TableCell>
                          <TableCell sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>
                            {formatUtcDateTime(conversation.lastMessageAt)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              )}
            </Panel>
          </Grid>

          <Grid size={{ xs: 12, lg: 5 }} sx={{ display: 'flex' }}>
            <Panel sx={{ flex: 1 }}>
              <Stack alignItems="center" direction="row" justifyContent="space-between">
                <Box>
                  <Typography component="h2" variant="h6">
                    Ask your data
                  </Typography>
                  <Typography color="text.secondary" variant="body2">
                    Natural-language analytics
                  </Typography>
                </Box>
                <Chip label="Not available yet" size="small" variant="outlined" />
              </Stack>
              <Divider />
              <Stack
                alignItems="center"
                justifyContent="center"
                spacing={1.5}
                sx={{ flex: 1, minHeight: 220, textAlign: 'center' }}
              >
                <DataObjectIcon aria-hidden="true" color="disabled" sx={{ fontSize: 40 }} />
                <Typography fontWeight={700}>Ask is not connected</Typography>
                <Typography color="text.secondary" sx={{ maxWidth: 360 }} variant="body2">
                  The canonical Bridge protocol does not expose a query service, so this dashboard
                  does not generate answers or accept prompts.
                </Typography>
              </Stack>
            </Panel>
          </Grid>
        </Grid>

        {!hasSnapshot && (
          <Stack
            alignItems="center"
            direction="row"
            role="status"
            spacing={1}
            sx={{ color: 'text.secondary' }}
          >
            <CircularProgress aria-hidden="true" size={16} />
            <Typography variant="body2">
              Loading dashboard. Waiting for the first analytics snapshot…
            </Typography>
          </Stack>
        )}
      </Stack>
    </Box>
  );
}
