import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { analyticsWindowLabel } from '../src/analytics';
import { GraphSummaryPanel } from '../src/components/graph';
import {
  storyAnalyticsModel,
  storyWindowSources,
} from '../src/story-only/analyticsFixtures';
import { theme } from '../src/theme';

afterEach(() => cleanup());

describe('relationship graph presentation', () => {
  it('shows canonical summary counts and gates unavailable query controls', () => {
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <GraphSummaryPanel
          summary={storyAnalyticsModel.graph}
          queryGate={{ enabled: false, reason: 'Bounded query API is not integrated.' }}
          windowSource={storyWindowSources.graph}
        />
      </ThemeProvider>,
    );

    expect(screen.getByText('84')).toBeTruthy();
    expect(screen.getByText('126')).toBeTruthy();
    expect(
      screen.getAllByText(`Data window: ${analyticsWindowLabel(storyWindowSources.graph)}`).length,
    ).toBe(4);
    expect(screen.getByText('Bounded query API is not integrated.')).toBeTruthy();
    expect((screen.getByLabelText('Relationship question') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Ask' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
