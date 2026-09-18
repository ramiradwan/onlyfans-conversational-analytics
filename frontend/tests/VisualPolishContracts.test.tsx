import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  formatCount,
  formatDecimal,
  formatRatioPercent,
  type AnalyticsReadState,
  type AnalyticsResponseMetrics,
} from '../src/analytics';
import { AnalyticsStateFrame, ResponseOverview } from '../src/components/analytics';
import { BrandMark } from '../src/layouts/BrandMark';
import { theme } from '../src/theme';
import { renderedText } from './renderedText';

afterEach(() => cleanup());

function renderWithTheme(element: ReactElement) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      {element}
    </ThemeProvider>,
  );
}

describe('frontend visual polish contracts', () => {
  it('uses the approved Conversation Analytics glyph inside the existing brand tile', () => {
    const { container } = renderWithTheme(<BrandMark />);

    const tile = container.querySelector('[data-visual="brand-tile"]');
    expect(tile).toBeTruthy();
    expect(tile?.querySelectorAll('svg[data-brand-mark="conversation-analytics"]')).toHaveLength(1);
    expect(screen.getByText('Conversation Analytics')).toBeTruthy();
  });

  it('presents reply statistics as three primary metrics with a supporting response count', () => {
    const metrics: AnalyticsResponseMetrics = {
      averageHandlingMinutes: 6.4,
      silencePercent: 0.25,
      turns: 31,
      responseCoverage: 0.75,
      responseOpportunityCount: 20,
      respondedCount: 15,
      provenance: {} as AnalyticsResponseMetrics['provenance'],
    };

    const { container } = renderWithTheme(<ResponseOverview metrics={metrics} />);

    expect(container.querySelectorAll('[data-visual="reply-metric-value"]')).toHaveLength(3);
    expect(screen.getByText('Average reply time').parentElement?.querySelector('dd')?.textContent).toBe(`${formatDecimal(6.4)} min`);
    expect(screen.getByText('Messages you replied to').parentElement?.querySelector('dd')?.textContent).toBe(formatRatioPercent(0.75));
    expect(screen.getByText(`${formatCount(15)} of ${formatCount(20)} messages`)).toBeTruthy();
    expect(screen.getByText(formatDecimal(31, 0))).toBeTruthy();
  });

  it('gives a terminal analytics error one direct recovery action', () => {
    const onRetry = vi.fn();
    const state: AnalyticsReadState = {
      status: 'error',
      data: null,
      isRefreshing: false,
      message: 'The analytics service did not respond.',
      previousStatus: null,
    };

    const { container } = renderWithTheme(<AnalyticsStateFrame state={state} onRetry={onRetry} />);

    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByRole('heading', { name: "Analytics couldn't load" })).toBeTruthy();
    expect(container.querySelector('[data-visual="analytics-empty-state"]')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('uses the existing fast and standard motion steps for disclosures', () => {
    expect(theme.components?.MuiCollapse?.defaultProps?.timeout).toEqual({ enter: 200, exit: 120 });
  });
});
