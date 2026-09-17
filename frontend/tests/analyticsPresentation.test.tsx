import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  analyticsWindowLabel,
  formatPercentValue,
  formatRatioPercent,
  formatSentimentScore,
  type AnalyticsReadState,
  type AnalyticsWindowSource,
  type AnalyticsWindowSources,
} from '../src/analytics';
import {
  AnalyticsFilterRow,
  AnalyticsPresentation,
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

const PANELS = ['Message tone over time', 'Your replies', 'Topics'] as const;

function withTheme(content: React.ReactNode) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      {content}
    </ThemeProvider>,
  );
}

function analytics(
  state: AnalyticsReadState,
  windowSources: AnalyticsWindowSources = storyWindowSources,
) {
  return withTheme(
    <AnalyticsPresentation
      state={state}
      dateRange={storyDateRange}
      onDateRangeChange={() => undefined}
      windowSources={windowSources}
    />,
  );
}

function withEffectiveWindow(
  source: AnalyticsWindowSource,
  start: string | null,
  end: string | null,
): AnalyticsWindowSource {
  return {
    ...source,
    provenance: {
      ...source.provenance,
      effective_window: { scope: 'effective', start, end },
    },
  };
}

afterEach(() => cleanup());

describe('analytics presentation states', () => {
  it('renders loading', () => {
    analytics({
      status: 'loading',
      data: null,
      isRefreshing: false,
      message: 'Loading your analytics…',
    });
    expect(screen.getByRole('status').textContent).toContain('Loading your analytics');
  });

  it('renders unavailable', () => {
    analytics({
      status: 'unavailable',
      data: null,
      isRefreshing: false,
      message: "Analytics aren't available for this account yet.",
    });
    expect(screen.getByText('Analytics are unavailable')).toBeTruthy();
  });

  it('labels baseline output explicitly', () => {
    analytics(storyBaselineState);
    expect(screen.getByText('Early estimates')).toBeTruthy();
    expect(screen.getByText(/early estimates\./)).toBeTruthy();
  });

  it('renders available output without a baseline label', () => {
    analytics(storyAvailableState);
    expect(screen.queryByText('Early estimates')).toBeNull();
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
  });

  it('keeps the prior frame visible while a filtered refetch is in progress', () => {
    const { container } = analytics({ ...storyAvailableState, isRefreshing: true });
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy();
    expect(screen.getByRole('progressbar', { name: 'Refreshing analytics' })).toBeTruthy();
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
  });

  it('renders error and preserves a prior complete frame when supplied', () => {
    analytics({
      status: 'error',
      data: storyAnalyticsModel,
      isRefreshing: false,
      message: "Your analytics couldn't be loaded.",
      previousStatus: 'model',
    });
    expect(screen.getByRole('alert').textContent).toContain("Couldn't refresh");
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
  });

  it('keeps the baseline disclosure when a failed refresh retains baseline data', () => {
    analytics({
      status: 'error',
      data: storyAnalyticsModel,
      isRefreshing: false,
      message: "Your analytics couldn't be loaded.",
      previousStatus: 'baseline',
    });
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain("Couldn't refresh");
    expect(alert.textContent).toContain('The early estimates below are still available.');
    expect(screen.queryByText('Early estimates')).toBeNull();
  });
});

describe('analytics dates, units and accessible trend detail', () => {
  it('keeps date fields as native editable date controls', () => {
    withTheme(
      <AnalyticsFilterRow
        value={storyDateRange}
        onApply={() => undefined}
      />,
    );

    for (const label of ['Start date', 'End date']) {
      const fields = screen.getAllByLabelText(label);
      expect(fields.length).toBeGreaterThan(0);
      for (const field of fields) {
        expect(field.getAttribute('type')).toBe('date');
        expect(field.closest('.MuiTextField-root')).toBeTruthy();
      }
    }
  });

  it('describes message dates in plain words', () => {
    const source = storyWindowSources.topics;
    expect(analyticsWindowLabel(source)).toMatch(/^Messages from .+ to .+$/);
    expect(
      analyticsWindowLabel(withEffectiveWindow(source, '2026-06-02T08:00:00Z', '2026-06-02T20:00:00Z')),
    ).toMatch(/^Messages from [^–]+$/);
    expect(analyticsWindowLabel(withEffectiveWindow(source, null, null))).toBe('No messages in these dates');
    expect(
      analyticsWindowLabel(withEffectiveWindow(storyWindowSources.graph, null, null)),
    ).toBe('No messages yet');
  });

  it('shows a shared date label once when every panel covers the same messages', () => {
    analytics(storyAvailableState);

    const label = analyticsWindowLabel(storyWindowSources.topics);
    expect(screen.getAllByText(label)).toHaveLength(1);
    for (const name of PANELS) {
      expect(within(screen.getByRole('region', { name })).queryByText(label)).toBeNull();
    }
    expect(screen.queryByText(/Data window/)).toBeNull();
  });

  it('labels each panel when the panels cover different messages', () => {
    const topics = withEffectiveWindow(
      storyWindowSources.topics,
      '2026-06-10T00:00:00.000Z',
      '2026-06-20T00:00:00.000Z',
    );
    analytics(storyAvailableState, { ...storyWindowSources, topics });

    const sharedLabel = analyticsWindowLabel(storyWindowSources.sentimentTrend);
    const topicsLabel = analyticsWindowLabel(topics);
    expect(topicsLabel).not.toBe(sharedLabel);
    const regions = {
      'Message tone over time': sharedLabel,
      'Your replies': sharedLabel,
      Topics: topicsLabel,
    };
    for (const [name, label] of Object.entries(regions)) {
      expect(within(screen.getByRole('region', { name })).getByText(label)).toBeTruthy();
    }
    expect(screen.getAllByText(sharedLabel)).toHaveLength(2);
  });

  it('keeps zero values visible and uses plain empty states', () => {
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
    };
    analytics({ ...storyAvailableState, data: zeroModel });

    const repliesPanel = screen.getByRole('region', { name: 'Your replies' });
    expect(within(repliesPanel).getByText(formatRatioPercent(0), { exact: true })).toBeTruthy();
    expect(within(repliesPanel).getByText('0 of 0 messages', { exact: true })).toBeTruthy();
    expect(within(repliesPanel).getByText('0', { exact: true })).toBeTruthy();
    expect(screen.getByText('Nothing to show for these dates.')).toBeTruthy();
    expect(screen.getByText('No topics found for these dates.')).toBeTruthy();
  });

  it('shows a dash for reply numbers that have no data', () => {
    analytics({
      ...storyAvailableState,
      data: {
        ...storyAnalyticsModel,
        response: {
          ...storyAnalyticsModel.response,
          averageHandlingMinutes: null,
          responseCoverage: null,
          turns: null,
        },
      },
    });

    const replies = within(screen.getByRole('region', { name: 'Your replies' }));
    expect(replies.getAllByText('—', { exact: true })).toHaveLength(3);
    expect(replies.queryByText(/Unavailable/)).toBeNull();
  });

  it('preserves percent units and displays sentiment on its signed range', () => {
    analytics(storyAvailableState);

    const topicView = screen.getByRole('list', { name: 'Topics and trend' });
    const responsePanel = screen.getByRole('region', { name: 'Your replies' });
    const sentimentPanel = screen.getByRole('region', {
      name: 'Message tone over time',
    });
    expect(topicView.textContent).toContain(formatPercentValue(37.5));
    expect(topicView.textContent).toContain(formatPercentValue(12.5));
    expect(within(responsePanel).getByText(formatRatioPercent(0.75), { exact: true })).toBeTruthy();
    expect(within(responsePanel).getByText('15 of 20 messages', { exact: true })).toBeTruthy();
    expect(responsePanel.textContent).not.toContain('Silence');
    expect(sentimentPanel.textContent).toContain(formatSentimentScore(0.35));
    expect(screen.queryByText('3,750.0%')).toBeNull();
    expect(topicView.textContent).not.toContain('Unavailable');
    expect(screen.queryByText(/bounded projection/)).toBeNull();
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
    const negativeMark = screen.getByRole('button', { name: /Negative tone/ });
    expect(negativeMark.getAttribute('data-hit-target')).toBe('24');
    fireEvent.focus(negativeMark);
    expect(screen.getByRole('tooltip').textContent).toContain('Negative tone');
    fireEvent.blur(negativeMark);

    const positiveMarks = screen.getAllByRole('button', { name: /Positive tone/ });
    fireEvent.mouseEnter(positiveMarks[0]);
    expect(screen.getByRole('tooltip').textContent).toContain('Positive tone');
    fireEvent.mouseLeave(positiveMarks[0]);

    fireEvent.click(screen.getByText('View data table'));
    expect(
      screen.getByRole('table', { name: 'Message tone over time data' }),
    ).toBeTruthy();
  });
});
