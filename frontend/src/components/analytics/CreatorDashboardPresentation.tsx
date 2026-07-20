import AccessTimeRoundedIcon from '@mui/icons-material/AccessTimeRounded';
import ForumRoundedIcon from '@mui/icons-material/ForumRounded';
import SentimentSatisfiedAltRoundedIcon from '@mui/icons-material/SentimentSatisfiedAltRounded';
import UpcomingRoundedIcon from '@mui/icons-material/UpcomingRounded';
import { Box, Stack, Typography, styled } from '@mui/material';

import { AnalyticsFilterRow } from './AnalyticsFilterRow';
import { AnalyticsStateFrame } from './AnalyticsStateFrame';
import { ChartPanel } from './ChartPanel';
import { MetricRow, type MetricCardProps } from './MetricCard';
import { ResponseOverview } from './ResponseOverview';
import { SentimentEngagementTrend } from './SentimentEngagementTrend';
import { TopicsTable } from './TopicsTable';
import {
  formatCount,
  formatDecimal,
  formatRatioPercent,
  formatSentimentScore,
  sentimentLabel,
  type AnalyticsDateRange,
  type AnalyticsReadModel,
  type AnalyticsReadState,
  type AnalyticsWindowSources,
} from '../../analytics';

const Root = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.default,
  flex: 1,
  minHeight: 0,
  overflowY: 'auto',
  padding: theme.spacing(2),
  [theme.breakpoints.up('md')]: {
    padding: theme.spacing(3),
  },
}));

const Bento = styled(Box)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(2),
  gridTemplateColumns: 'minmax(0, 1fr)',
  [theme.breakpoints.up('lg')]: {
    gridTemplateColumns: 'minmax(0, 2fr) minmax(17rem, 1fr)',
  },
}));

const TopicsCell = styled(Box)(({ theme }) => ({
  minWidth: 0,
  [theme.breakpoints.up('lg')]: {
    gridColumn: '1 / -1',
  },
}));

function metricItems(
  model: AnalyticsReadModel,
  windowSources: AnalyticsWindowSources,
): readonly MetricCardProps[] {
  const creator = model.creator;
  const sentiment = creator?.averageSentimentScore ?? null;
  return [
    {
      label: 'Conversations',
      value: creator ? formatCount(creator.conversationCount) : 'Unavailable',
      supportingText: creator
        ? `${formatCount(creator.participantCount)} canonical participants`
        : 'Creator aggregate is not in this projection',
      icon: <ForumRoundedIcon />,
      tone: 'connection',
      windowSource: windowSources.creatorMetrics,
    },
    {
      label: 'Average sentiment',
      value:
        sentiment === null
          ? 'Unavailable'
          : `${sentimentLabel(sentiment)} · ${formatSentimentScore(sentiment)}`,
      supportingText: 'Directional score on a −1 to +1 range',
      icon: <SentimentSatisfiedAltRoundedIcon />,
      tone: 'sentiment',
      windowSource: windowSources.creatorMetrics,
    },
    {
      label: 'Average handling time',
      value: `${formatDecimal(model.response.averageHandlingMinutes)} min`,
      supportingText: 'Observed creator responses only',
      icon: <AccessTimeRoundedIcon />,
      tone: 'primary',
      windowSource: windowSources.responseMetrics,
    },
    {
      label: 'Response opportunities',
      value: formatCount(model.response.responseOpportunityCount),
      supportingText: `${formatCount(model.response.respondedCount)} responded · ${formatRatioPercent(model.response.responseCoverage)} coverage`,
      icon: <UpcomingRoundedIcon />,
      tone: 'opportunity',
      windowSource: windowSources.responseMetrics,
    },
  ];
}
export interface CreatorDashboardPresentationProps {
  state: AnalyticsReadState;
  dateRange: AnalyticsDateRange;
  onDateRangeChange(range: AnalyticsDateRange): void;
  windowSources?: AnalyticsWindowSources;
}

export function CreatorDashboardPresentation({
  state,
  dateRange,
  onDateRangeChange,
  windowSources,
}: CreatorDashboardPresentationProps) {
  const model = state.data;
  const resolvedWindowSources = windowSources ?? model?.windowSources;
  return (
    <Root>
      <Stack spacing={2.5}>
        <Box>
          <Typography component="h1" variant="h4">
            Conversation overview
          </Typography>
          <Typography component="p" variant="body1" color="text.secondary">
            Date controls request a range; each result states its confirmed data window.
          </Typography>
        </Box>
        <AnalyticsFilterRow
          value={dateRange}
          onApply={onDateRangeChange}
          isRefreshing={state.isRefreshing}
        />
        <AnalyticsStateFrame state={state}>
          {model && (
            <Stack spacing={2}>
              <MetricRow items={metricItems(model, resolvedWindowSources!)} />
              <Bento>
                <ChartPanel
                  title="Sentiment over time"
                  description="Daily baseline score; polarity is encoded by label, symbol, and color."
                  windowSource={resolvedWindowSources!.sentimentTrend}
                >
                  <SentimentEngagementTrend sentiment={model.sentimentTrend} />
                </ChartPanel>
                <ChartPanel
                  title="Response health"
                  description="Counts and units are preserved from the canonical projection."
                  windowSource={resolvedWindowSources!.responseMetrics}
                >
                  <ResponseOverview metrics={model.response} />
                </ChartPanel>
                <TopicsCell>
                  <ChartPanel
                    title="Leading topics"
                    description="Compact magnitude table; percentages are already expressed on a 0–100 scale."
                    windowSource={resolvedWindowSources!.topics}
                  >
                    <TopicsTable topics={model.topics.slice(0, 7)} />
                  </ChartPanel>
                </TopicsCell>
              </Bento>
            </Stack>
          )}
        </AnalyticsStateFrame>
      </Stack>
    </Root>
  );
}
