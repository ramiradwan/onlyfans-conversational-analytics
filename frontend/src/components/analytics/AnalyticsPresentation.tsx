import { Box, Stack, Tab, Tabs, Typography, styled } from '@mui/material';
import { useId, useState, type ReactNode } from 'react';

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
  paddingBottom: theme.spacing(4),
}));

const ControlBar = styled(Box)(({ theme }) => ({
  alignItems: 'stretch',
  borderBottom: `1px solid ${theme.vars.palette.divider}`,
  display: 'flex',
  flexDirection: 'column',
  gap: theme.spacing(1.5),
  [theme.breakpoints.up('md')]: {
    alignItems: 'center',
    flexDirection: 'row',
    justifyContent: 'space-between',
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
  recurringMentions?: ReactNode;
}
export function AnalyticsPresentation({
  state,
  dateRange,
  onDateRangeChange,
  onRetry,
  windowSources,
  questions,
  findings,
  recurringMentions,
}: AnalyticsPresentationProps) {
  const [tab, setTab] = useState<'summary' | 'questions' | 'findings' | 'recurring'>('summary');
  const tabsId = useId();
  const activeTab = (tab === 'questions' && !questions) || (tab === 'findings' && !findings)
    || (tab === 'recurring' && !recurringMentions) ? 'summary' : tab;
  const model = state.data;
  const resolvedWindowSources = windowSources ?? model?.windowSources;
  const panelSources = resolvedWindowSources && {
    sentimentTrend: resolvedWindowSources.sentimentTrend,
    responseMetrics: resolvedWindowSources.responseMetrics,
    topics: resolvedWindowSources.topics,
  };
  const sharedSource = panelSources ? sharedWindowSource(Object.values(panelSources)) : null;
  const perPanel = sharedSource ? undefined : panelSources;
  const hasTabs = Boolean(questions || findings || recurringMentions);
  const showDateFilter = activeTab === 'summary' || activeTab === 'findings';

  return (
    <Root data-scroll-container>
      <Stack
        data-visual="analytics-frame"
        spacing={2.5}
        sx={{ maxWidth: componentTokens.shell.dashboardMaxWidth, mx: 'auto', width: '100%' }}
      >
        <Box>
          <Typography component="h1" variant="h4">
            Analytics
          </Typography>
          <Typography component="p" variant="body1" sx={{ color: 'text.secondary', mt: 0.5 }}>
            Message tone, your replies, and what conversations are about.
          </Typography>
        </Box>

        <ControlBar>
          {hasTabs ? (
            <Tabs
              value={activeTab}
              onChange={(_, value) => setTab(value)}
              aria-label="Analytics views"
              variant="scrollable"
              scrollButtons="auto"
              sx={{
                minHeight: 44,
                mb: { md: '-1px' },
                '& .MuiTab-root': { minHeight: 44, py: 1 },
              }}
            >
              <Tab value="summary" label="Summary" id={`${tabsId}-summary`} aria-controls={tabsId} />
              {questions && <Tab value="questions" label="Conversation questions" id={`${tabsId}-questions`} aria-controls={tabsId} />}
              {findings && <Tab value="findings" label="Text analysis" id={`${tabsId}-findings`} aria-controls={tabsId} />}
              {recurringMentions && <Tab value="recurring" label="Recurring mentions" id={`${tabsId}-recurring`} aria-controls={tabsId} />}
            </Tabs>
          ) : (
            <Box />
          )}

          {showDateFilter && (
            <Box sx={{ pb: { xs: 1.5, md: 1 }, flexShrink: 0 }}>
              <AnalyticsFilterRow
                value={dateRange}
                onApply={onDateRangeChange}
                isRefreshing={state.isRefreshing}
              />
            </Box>
          )}
        </ControlBar>

        <Box id={hasTabs ? tabsId : undefined} role={hasTabs ? 'tabpanel' : undefined}
          aria-labelledby={hasTabs ? `${tabsId}-${activeTab}` : undefined}>
        {activeTab === 'questions' ? (
          questions
        ) : activeTab === 'findings' ? (
          findings
        ) : activeTab === 'recurring' ? (
          recurringMentions
        ) : (
          // Holds the results at one size, so loading, refreshing and new results swap inside it. The panels stack in one
          // column below md.
          <Results><ReservedRegion id="analytics-content" regionRole="scroll" size={resultsSize(topicRegionSize.list)} sx={resultsWithTopicTable}>
          <Box key={state.data ? 'results' : state.status} data-region-content>
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
          </Box>
          </ReservedRegion></Results>
        )}
        </Box>
      </Stack>
    </Root>
  );
}
