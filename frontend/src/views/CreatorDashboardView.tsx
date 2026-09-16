import DataObjectIcon from '@mui/icons-material/DataObject';
import {
  Alert,
  AlertTitle,
  Box,
  Button,
  CircularProgress,
  Collapse,
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
import { LineChart, lineClasses } from '@mui/x-charts/LineChart';
import { PieChart } from '@mui/x-charts/PieChart';
import { useId, useMemo, useState, useSyncExternalStore } from 'react';

import {
  buildCreatorDashboardModel,
  type MessageActivityPoint,
  type Sentiment,
} from './creatorDashboardModel';
import { KpiCard } from '../components/KpiCard';
import { SetupPrompt } from '../components/SetupPrompt';
import { Panel } from '../components/ui';
import type { ProjectionState } from '../protocol';
import {
  bridgeTransportStore,
  type BridgeTransportState,
} from '../store/transportStore';
import { componentTokens } from '../theme/generated/tokens';
import {
  formatAdditiveMetric,
  humanizeCoverageReason,
  humanizeProjectionReason,
  isConfigurationAligned,
  summarizeMetricEvidence,
  type MetricEvidence,
} from '../utils/dataReadiness';
import {
  extensionConnection,
  extensionIssue,
  protocolErrorText,
  setupIncomplete,
  type StatusMessage,
} from '../utils/statusCopy';

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
      <Skeleton height="60%" variant="rounded" animation={false} />
      <Skeleton width="100%" animation={false} />
    </Stack>
  );
}

function EmptyChart({ children }: { children: string }) {
  return (
    <Stack
      sx={{
        alignItems: 'center',
        justifyContent: 'center',
        color: 'text.secondary',
        minHeight: CHART_HEIGHT,
        textAlign: 'center'
      }}>
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

const LOCAL_DATE_TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
});
const LOCAL_DATE_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });

function formatLocal(date: string | null, format: Intl.DateTimeFormat): string | null {
  if (date === null) return null;
  const timestamp = Date.parse(date);
  return Number.isFinite(timestamp) ? format.format(timestamp) : null;
}

function NumbersBasis({
  evidence,
  messagesCounted,
  projection,
}: {
  evidence: MetricEvidence;
  messagesCounted: number | null;
  projection: Pick<ProjectionState, 'canonical_revision'>;
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
    <Stack spacing={1}>
      <Stack
        direction="row"
        spacing={1}
        sx={{ alignItems: 'center', columnGap: 1, flexWrap: 'wrap', rowGap: 0.5 }}
      >
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          {evidence.partial
            ? 'History is still syncing, so these numbers will grow.'
            : 'Based on your full message history.'}
          {updated ? ` Updated ${updated}.` : ''}
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
    </Stack>
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
  const detailedAnalyticsAvailable = false;
  const issue = getIssue(state);
  const kpis = [
    { title: 'Total conversations', value: model.analytics?.total_conversations },
    { title: 'Total messages', value: model.analytics?.total_messages },
    { title: 'Inbound messages', value: model.analytics?.inbound_messages },
    { title: 'Outbound messages', value: model.analytics?.outbound_messages },
  ];
  const sentimentTotal = model.sentimentCounts.reduce((total, item) => total + item.count, 0);
  const showSetup = hasSnapshot && setupIncomplete(state.coverage);
  const hasCounts = kpis.some((kpi) => (kpi.value?.value ?? 0) > 0);
  const showNumbers = !showSetup || hasCounts;
  const evidence = summarizeMetricEvidence(kpis.map((kpi) => kpi.value));

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
        <Typography component="h1" variant="h4">
          Dashboard
        </Typography>

        {issue !== null && (
          <Alert severity={issue.severity} role="alert">
            <AlertTitle>{issue.title}</AlertTitle>
            {issue.detail}
          </Alert>
        )}

        {showSetup && <SetupPrompt title="Finish setup to see your numbers" />}

        {showNumbers && (
        <Grid container spacing={{ xs: 2, md: 3 }} aria-busy={!hasSnapshot}>
          {kpis.map((kpi) => (
            <Grid key={kpi.title} size={{ xs: 12, sm: 6, lg: 3 }}>
              <KpiCard
                grow
                isLoading={!hasSnapshot}
                title={kpi.title}
                value={formatAdditiveMetric(kpi.value, readiness, (value) =>
                  NUMBER_FORMAT.format(value),
                )}
              />
            </Grid>
          ))}

          {hasSnapshot && evidence !== null && (
            <Grid size={12}>
              <NumbersBasis
                evidence={evidence}
                messagesCounted={model.analytics?.total_messages?.sample_size ?? null}
                projection={state.projection}
              />
            </Grid>
          )}

          {detailedAnalyticsAvailable && (
          <>
          <Grid size={{ xs: 12, lg: 8 }} sx={{ display: 'flex' }}>
            <Panel sx={{ flex: 1, minWidth: 0 }}>
              <Box>
                <Typography component="h2" id="message-activity-title" variant="h6">
                  Message activity
                </Typography>
                <Typography variant="body2" sx={{
                  color: 'text.secondary'
                }}>
                  Daily inbound and outbound messages in UTC
                </Typography>
              </Box>

              {!hasSnapshot ? (
                <ChartSkeleton />
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
                        [`& .${lineClasses.line}`]: { strokeWidth: 2 },
                        [`& .${lineClasses.mark}`]: {
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
                    id="message-activity-description"
                    variant="caption"
                    sx={{
                      color: 'text.secondary',
                      mb: 0.5
                    }}>
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
                <Typography variant="body2" sx={{
                  color: 'text.secondary'
                }}>
                  All message sentiment classifications
                </Typography>
              </Box>

              {!hasSnapshot ? (
                <ChartSkeleton />
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
                          faded: {
                            additionalRadius: -3,
                            color: 'var(--bridge-palette-text-disabled)',
                          },
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
                            <Stack direction="row" spacing={1} sx={{
                              alignItems: 'center'
                            }}>
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
                <Typography variant="body2" sx={{
                  color: 'text.secondary'
                }}>
                  Ranked by message count, then latest activity and conversation ID
                </Typography>
              </Box>

              {!hasSnapshot ? (
                <Stack aria-label="Loading active conversations" spacing={1.5}>
                  {[1, 2, 3, 4, 5].map((row) => (
                    <Skeleton height={48} key={row} variant="rounded" />
                  ))}
                </Stack>
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
                            <Typography variant="body2" sx={{
                              fontWeight: 700
                            }}>
                              {conversation.displayName}
                            </Typography>
                            <Typography variant="caption" sx={{
                              color: 'text.secondary'
                            }}>
                              {conversation.platformUserId}
                            </Typography>
                          </TableCell>
                          <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                            <Typography component="span" variant="body2" sx={{
                              fontWeight: 700
                            }}>
                              {NUMBER_FORMAT.format(conversation.messageCount)}
                            </Typography>
                            <Typography
                              variant="caption"
                              sx={{
                                color: 'text.secondary',
                                display: 'block'
                              }}>
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

          </>
          )}
        </Grid>
        )}

        {!hasSnapshot && (
          <Stack
            direction="row"
            role="status"
            spacing={1}
            sx={{
              alignItems: 'center',
              color: 'text.secondary'
            }}>
            <CircularProgress aria-hidden="true" size={16} />
            <Typography variant="body2">
              Loading your numbers…
            </Typography>
          </Stack>
        )}
      </Stack>
    </Box>
  );
}
