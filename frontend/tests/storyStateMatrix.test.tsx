import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  storyAnalyticsState,
  storyAnalyticsStateOptions,
} from '../src/story-only/analyticsFixtures';
import {
  StoryAnalyticsView,
  StoryDashboardView,
  StoryGraphView,
  StoryInboxView,
} from '../src/story-only/StoryViews';
import { theme } from '../src/theme';

const storyViews = [
  StoryDashboardView,
  StoryAnalyticsView,
  StoryInboxView,
  StoryGraphView,
] as const;

describe('complete story state matrix', () => {
  it('renders every selectable state in every view without duplicate IDs', () => {
    expect(storyAnalyticsStateOptions.map((option) => option.key)).toEqual([
      'loading',
      'unavailable',
      'building',
      'baseline',
      'model',
      'error',
    ]);

    for (const option of storyAnalyticsStateOptions) {
      for (const StoryComponent of storyViews) {
        const { container } = render(
          <ThemeProvider theme={theme} defaultMode="light">
            <StoryComponent state={storyAnalyticsState(option.key)} />
          </ThemeProvider>,
        );
        const ids = Array.from(container.querySelectorAll<HTMLElement>('[id]')).map(
          (element) => element.id,
        );
        expect(new Set(ids).size).toBe(ids.length);
        expect(container.querySelectorAll('h1').length).toBe(1);
        cleanup();
      }
    }
  });
});
