import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  analyticsWindowLabel,
  formatPercentValue,
  formatRatioPercent,
  formatSentimentScore,
  type AnalyticsReadState,
  type AnalyticsWindowSources,
} from '../src/analytics';
import {
  AnalyticsFilterRow,
  AnalyticsPresentation,
  CreatorDashboardPresentation,
  SentimentEngagementTrend,
} from '../src/components/analytics';
import {
  storyAnalyticsModel,
  storyAvailableState,
  storyBaselineState,
  storyDateRange,
  storyWindowSources,
} from '../src/story-only/analyticsFixtures';
import { theme } from '../src/theme';

const effectiveWindowLabel = analyticsWindowLabel(storyWindowSources.creatorMetrics);

function withTheme(content: React.ReactNode) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      {content}
    </ThemeProvider>,
  );
}

function dashboard(
  state: AnalyticsReadState,
  windowSources: AnalyticsWindowSources = storyWindowSources,
) {
  return withTheme(
    <CreatorDashboardPresentation
      state={state}
      dateRange={storyDateRange}
      onDateRangeChange={() => undefined}
      windowSources={windowSources}
    />,
  );
}

afterEach(() => cleanup());

describe('analytics presentation states', () => {
  it('renders loading', () => {
    dashboard({
      status: 'loading',
      data: null,
      isRefreshing: false,
      message: 'Loading canonical analytics…',
    });
    expect(screen.getByRole('status').textContent).toContain('Loading canonical analytics');
  });

  it('renders unavailable', () => {
    dashboard({
      status: 'unavailable',
      data: null,
      isRefreshing: false,
      message: 'Canonical analytics are not available yet.',
    });
    expect(screen.getByText('Analytics are unavailable')).toBeTruthy();
  });

  it('labels baseline output explicitly', () => {
    dashboard(storyBaselineState);
    expect(screen.getByText('Directional baseline')).toBeTruthy();
    expect(screen.getByText(/not calibrated production analysis/)).toBeTruthy();
  });

  it('renders available output without a baseline label', () => {
    dashboard(storyAvailableState);
    expect(screen.queryByText('Directional baseline')).toBeNull();
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
  });

  it('keeps the prior frame visible while a filtered refetch is in progress', () => {
    const { container } = dashboard({ ...storyAvailableState, isRefreshing: true });
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy();
    expect(screen.getByRole('progressbar', { name: 'Refreshing analytics' })).toBeTruthy();
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
  });

  it('renders error and preserves a prior complete frame when supplied', () => {
    dashboard({
      status: 'error',
      data: storyAnalyticsModel,
      isRefreshing: false,
      message: 'Canonical analytics could not be loaded.',
      previousStatus: 'model',
    });
    expect(screen.getByRole('alert').textContent).toContain('Refresh failed');
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
  });

  it('keeps the baseline disclosure when a failed refresh retains baseline data', () => {
    dashboard({
      status: 'error',
      data: storyAnalyticsModel,
      isRefreshing: false,
      message: 'Canonical analytics could not be loaded.',
      previousStatus: 'baseline',
    });
    expect(screen.getByText('Directional baseline')).toBeTruthy();
    expect(screen.getByText(/retained frame is a directional baseline/)).toBeTruthy();
  });
});

describe('analytics units and accessible trend detail', () => {
  it('keeps date fields as native editable date controls', () => {
    withTheme(
      <AnalyticsFilterRow
        value={storyDateRange}
        onApply={() => undefined}
      />,
    );

    for (const field of [screen.getByLabelText('Start date'), screen.getByLabelText('End date')]) {
      expect(field.getAttribute('type')).toBe('date');
      expect(field.closest('.MuiTextField-root')).toBeTruthy();
    }
  });

  it('labels confirmed effective windows without inferring scope from request success', () => {
    dashboard(storyAvailableState, storyWindowSources);

    const expected = `Data window: ${effectiveWindowLabel}`;
    expect(
      within(screen.getByRole('group', { name: 'Conversations metric' })).getByText(expected),
    ).toBeTruthy();
    expect(
      within(screen.getByRole('group', { name: 'Average handling time metric' })).getByText(expected),
    ).toBeTruthy();
    expect(
      within(screen.getByRole('region', { name: 'Sentiment over time' })).getByText(expected),
    ).toBeTruthy();
    expect(
      within(screen.getByRole('region', { name: 'Leading topics' })).getByText(expected),
    ).toBeTruthy();
    expect(screen.queryByText(/selected account and date range/i)).toBeNull();
  });

  it('keeps zero values visible while unknown windows remain explicitly unavailable', () => {
    const zeroModel = {
      ...storyAnalyticsModel,
      topics: [],
      sentimentTrend: [],
      response: {
        ...storyAnalyticsModel.response,
        averageHandlingMinutes: 0,
        silencePercent: 0,
        turns: 0,
        responseCoverage: 0,
        responseOpportunityCount: 0,
        respondedCount: 0,
      },
      creator: {
        ...storyAnalyticsModel.creator,
        conversationCount: 0,
        participantCount: 0,
        messageCount: 0,
        inboundMessageCount: 0,
        outboundMessageCount: 0,
        averageMessagesPerConversation: 0,
        averageResponseSeconds: null,
        averageSentimentScore: 0,
        responseCoverage: 0,
      },
    };
    dashboard({ ...storyAvailableState, data: zeroModel });

    expect(
      within(screen.getByRole('group', { name: 'Conversations metric' })).getByText('0'),
    ).toBeTruthy();
    expect(
      screen.getAllByText(`Data window: ${effectiveWindowLabel}`).length,
    ).toBe(7);
  });

  it('preserves percent units and displays sentiment on its signed range', () => {
    withTheme(
      <AnalyticsPresentation
        state={storyAvailableState}
        dateRange={storyDateRange}
        onDateRangeChange={() => undefined}
        windowSources={storyWindowSources}
      />,
    );

    const topicTable = screen.getByRole('table', { name: 'Topic magnitude and trend' });
    const responsePanel = screen.getByRole('region', { name: 'Response metrics' });
    const sentimentPanel = screen.getByRole('region', {
      name: 'Sentiment and engagement trend',
    });
    expect(topicTable.textContent).toContain(formatPercentValue(37.5));
    expect(topicTable.textContent).toContain(formatPercentValue(12.5));
    expect(screen.getByText(formatPercentValue(22.5))).toBeTruthy();
    expect(responsePanel.textContent).toContain(formatRatioPercent(0.75));
    expect(sentimentPanel.textContent).toContain(formatSentimentScore(0.35));
    expect(screen.queryByText('3,750.0%')).toBeNull();
  });

  it('shows chart values on hover and keyboard focus and exposes a table view', () => {
    const engagement = storyAnalyticsModel.sentimentTrend.map((point, index) => ({
      ...point,
      value: 0.5 + index * 0.08,
    }));
    withTheme(
      <SentimentEngagementTrend
        sentiment={storyAnalyticsModel.sentimentTrend}
        engagement={engagement}
      />,
    );

    expect(screen.getByLabelText('Chart legend')).toBeTruthy();
    const negativeMark = screen.getByRole('button', { name: /Negative sentiment/ });
    expect(negativeMark.getAttribute('data-hit-target')).toBe('24');
    fireEvent.focus(negativeMark);
    expect(screen.getByRole('tooltip').textContent).toContain('Negative sentiment');
    fireEvent.blur(negativeMark);

    const positiveMarks = screen.getAllByRole('button', { name: /Positive sentiment/ });
    fireEvent.mouseEnter(positiveMarks[0]);
    expect(screen.getByRole('tooltip').textContent).toContain('Positive sentiment');
    fireEvent.mouseLeave(positiveMarks[0]);

    fireEvent.click(screen.getByText('View data table'));
    expect(
      screen.getByRole('table', { name: 'Sentiment and engagement trend data' }),
    ).toBeTruthy();
  });
});
