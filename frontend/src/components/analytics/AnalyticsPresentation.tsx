import { Box, Stack, Typography, styled } from '@mui/material';

import { AnalyticsFilterRow } from './AnalyticsFilterRow';
import { AnalyticsStateFrame } from './AnalyticsStateFrame';
import { ChartPanel } from './ChartPanel';
import { ResponseOverview } from './ResponseOverview';
import { SentimentEngagementTrend } from './SentimentEngagementTrend';
import { TopicsTable } from './TopicsTable';
import type {
  AnalyticsDateRange,
  AnalyticsReadState,
  AnalyticsWindowSources,
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

const AnalyticsGrid = styled(Box)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(2),
  gridTemplateColumns: 'minmax(0, 1fr)',
  [theme.breakpoints.up('md')]: {
    gridTemplateColumns: 'minmax(0, 2fr) minmax(17rem, 1fr)',
  },
}));

const FullWidth = styled(Box)(({ theme }) => ({
  minWidth: 0,
  [theme.breakpoints.up('md')]: {
    gridColumn: '1 / -1',
  },
}));

export interface AnalyticsPresentationProps {
  state: AnalyticsReadState;
  dateRange: AnalyticsDateRange;
  onDateRangeChange(range: AnalyticsDateRange): void;
  windowSources?: AnalyticsWindowSources;
}
export function AnalyticsPresentation({
  state,
  dateRange,
  onDateRangeChange,
  windowSources,
}: AnalyticsPresentationProps) {
  const model = state.data;
  const resolvedWindowSources = windowSources ?? model?.windowSources;
  return (
    <Root>
      <Stack spacing={2.5}>
        <Box>
          <Typography component="h1" variant="h4">
            Analytics
          </Typography>
          <Typography component="p" variant="body1" sx={{
            color: 'text.secondary'
          }}>
            Message tone, your replies, and what conversations are about.
          </Typography>
        </Box>
        <AnalyticsFilterRow
          value={dateRange}
          onApply={onDateRangeChange}
          isRefreshing={state.isRefreshing}
        />
        <AnalyticsStateFrame state={state}>
          {model && (
            <AnalyticsGrid>
              <ChartPanel
                title="Message tone over time"
                description="Average tone of messages each day, from −1 (negative) to +1 (positive)."
                windowSource={resolvedWindowSources!.sentimentTrend}
              >
                <SentimentEngagementTrend sentiment={model.sentimentTrend} />
              </ChartPanel>
              <ChartPanel
                title="Your replies"
                description="How often and how quickly you reply."
                windowSource={resolvedWindowSources!.responseMetrics}
              >
                <ResponseOverview metrics={model.response} />
              </ChartPanel>
              <FullWidth>
                <ChartPanel
                  title="Topics"
                  description="What conversations are about."
                  windowSource={resolvedWindowSources!.topics}
                >
                  <TopicsTable topics={model.topics} />
                </ChartPanel>
              </FullWidth>
            </AnalyticsGrid>
          )}
        </AnalyticsStateFrame>
      </Stack>
    </Root>
  );
}
