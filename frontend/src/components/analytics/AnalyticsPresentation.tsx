import { Box, Stack, Tab, Tabs, Typography, styled } from '@mui/material';
import { useState, type ReactNode } from 'react';

import { AnalyticsFilterRow } from './AnalyticsFilterRow';
import { AnalyticsStateFrame } from './AnalyticsStateFrame';
import { AnalyticsWindowLabel } from './AnalyticsWindowLabel';
import { ChartPanel } from './ChartPanel';
import { ResponseOverview } from './ResponseOverview';
import { SentimentEngagementTrend } from './SentimentEngagementTrend';
import { TopicsTable, topicRegionSize } from './TopicsTable';
import {
  analyticsWindowLabel,
  type AnalyticsDateRange,
  type AnalyticsReadState,
  type AnalyticsWindowSource,
  type AnalyticsWindowSources,
} from '../../analytics';
import { componentTokens, layoutTokens } from '../../theme';
import { ReservedRegion } from '../ui/ReservedRegion';

const Root = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.default,
  flex: 1,
  minHeight: 0,
  overflowY: 'auto',
  paddingBottom: theme.spacing(3),
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

const Results = styled(Box)({ containerName: 'analytics-results', containerType: 'inline-size' });

// The topics table fits once the results, less their 10px scrollbar and the panel's padding, hold topicTableMinWidth.
const topicTableFits = `@container analytics-results (min-width: calc(${componentTokens.analytics.topicTableMinWidth} + ${layoutTokens.spacingUnit * 5 + 10}px))`;
// The results hold the topics region and a rem allowance for each layout's other content.
const resultsSize = (topics: string) => ({ xs: `calc(86.5rem + ${topics})`, sm: `calc(77rem + ${topics})`, md: `calc(51rem + ${topics})` });
const resultsWithTopicTable = { [topicTableFits]: { blockSize: resultsSize(topicRegionSize.table) } };

/** Returns the source shared by every panel, or null when panel windows differ. */
function sharedWindowSource(sources: AnalyticsWindowSource[]): AnalyticsWindowSource | null {
  const labels = new Set(sources.map(analyticsWindowLabel));
  return labels.size === 1 ? sources[0] : null;
}

export interface AnalyticsPresentationProps {
  state: AnalyticsReadState;
  dateRange: AnalyticsDateRange;
  onDateRangeChange(range: AnalyticsDateRange): void;
  onRetry?: () => void;
  windowSources?: AnalyticsWindowSources;
  questions?: ReactNode;
  findings?: ReactNode;
}
export function AnalyticsPresentation({
  state,
  dateRange,
  onDateRangeChange,
  onRetry,
  windowSources,
  questions,
  findings,
}: AnalyticsPresentationProps) {
  const [tab, setTab] = useState<'summary' | 'questions' | 'findings'>('summary');
  const model = state.data;
  const resolvedWindowSources = windowSources ?? model?.windowSources;
  const panelSources = resolvedWindowSources && {
    sentimentTrend: resolvedWindowSources.sentimentTrend,
    responseMetrics: resolvedWindowSources.responseMetrics,
    topics: resolvedWindowSources.topics,
  };
  const sharedSource = panelSources ? sharedWindowSource(Object.values(panelSources)) : null;
  const perPanel = sharedSource ? undefined : panelSources;
  return (
    <Root>
      <Stack
        data-visual="analytics-frame"
        spacing={2.5}
        sx={{ maxWidth: componentTokens.shell.dashboardMaxWidth, mx: 'auto', width: '100%' }}
      >
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
        {(questions || findings) && <Tabs value={tab} onChange={(_, value) => setTab(value)} aria-label="Analytics views">
          <Tab value="summary" label="Summary" />
          {questions && <Tab value="questions" label="Conversation questions" />}
          {findings && <Tab value="findings" label="Text analysis" />}
        </Tabs>}
        {questions && tab === 'questions' ? questions : <>
        <AnalyticsFilterRow
          value={dateRange}
          onApply={onDateRangeChange}
          isRefreshing={state.isRefreshing}
        />
        {findings && tab === 'findings' ? findings : <>
        {/* Holds the results. The panels stack in one column below md. */}
        <Results><ReservedRegion id="analytics-content" regionRole="scroll" size={resultsSize(topicRegionSize.list)} sx={resultsWithTopicTable}><Box key={state.data ? 'results' : state.status} data-region-content>
        <AnalyticsStateFrame state={state} onRetry={onRetry}>
          {model && (
            <Stack spacing={1.5}>
              {sharedSource && <AnalyticsWindowLabel source={sharedSource} />}
              <AnalyticsGrid>
                <ChartPanel
                  emphasis="dominant"
                  title="Message tone over time"
                  description="Average tone of messages each day, from −1 (negative) to +1 (positive)."
                  windowSource={perPanel?.sentimentTrend}
                >
                  <SentimentEngagementTrend sentiment={model.sentimentTrend} />
                </ChartPanel>
                <ChartPanel
                  title="Your replies"
                  description="How often and how quickly you reply."
                  windowSource={perPanel?.responseMetrics}
                >
                  <ResponseOverview metrics={model.response} />
                </ChartPanel>
                <FullWidth>
                  <ChartPanel
                    title="Topics"
                    description="What conversations are about."
                    windowSource={perPanel?.topics}
                  >
                    <TopicsTable topics={model.topics} />
                  </ChartPanel>
                </FullWidth>
              </AnalyticsGrid>
            </Stack>
          )}
        </AnalyticsStateFrame>
        </Box></ReservedRegion></Results>
        </>}
        </>}
      </Stack>
    </Root>
  );
}
