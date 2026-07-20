import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { storyAvailableState } from '../src/story-only/analyticsFixtures';
import {
  StoryAnalyticsView,
  StoryDashboardView,
  StoryGraphView,
  StoryInboxView,
} from '../src/story-only/StoryViews';
import { theme } from '../src/theme';

afterEach(() => cleanup());

const stories = [
  ['dashboard', <StoryDashboardView state={storyAvailableState} />],
  ['analytics', <StoryAnalyticsView state={storyAvailableState} />],
  ['inbox', <StoryInboxView state={storyAvailableState} />],
  ['graph', <StoryGraphView state={storyAvailableState} />],
] as const;

describe('story document outlines', () => {
  it.each(stories)('%s has one ordered outline and no duplicate IDs', (_name, story) => {
    const { container } = render(
      <ThemeProvider theme={theme} defaultMode="light">
        {story}
      </ThemeProvider>,
    );

    const headings = Array.from(
      container.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6'),
    );
    const levels = headings.map((heading) => Number(heading.tagName.slice(1)));
    expect(levels[0]).toBe(1);
    expect(levels.filter((level) => level === 1).length).toBe(1);
    for (let index = 1; index < levels.length; index += 1) {
      expect(levels[index]).toBeLessThanOrEqual(levels[index - 1] + 1);
    }

    const ids = Array.from(container.querySelectorAll<HTMLElement>('[id]')).map(
      (element) => element.id,
    );
    expect(new Set(ids).size).toBe(ids.length);
  });
});
