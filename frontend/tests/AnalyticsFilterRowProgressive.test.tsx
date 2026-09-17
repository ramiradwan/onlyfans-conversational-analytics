import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AnalyticsFilterRow } from '../src/components/analytics';
import { theme } from '../src/theme';

const RANGE = { startDate: '2026-06-01', endDate: '2026-06-30' };

afterEach(() => cleanup());

describe('AnalyticsFilterRow progressive disclosure', () => {
  it('keeps detailed dates behind one compact range control', () => {
    const onApply = vi.fn();
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <AnalyticsFilterRow value={RANGE} onApply={onApply} />
      </ThemeProvider>,
    );

    const trigger = screen.getByRole('button', { name: 'Change dates' });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect((screen.getByLabelText('Start date') as HTMLInputElement).type).toBe('date');
    expect((screen.getByLabelText('End date') as HTMLInputElement).type).toBe('date');

    fireEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('button', { name: 'Last 30 days' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Last 90 days' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'All time' }));
    expect(onApply).toHaveBeenCalledWith({ startDate: '', endDate: '' });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
  });
});
