import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AnalyticsFilterRow } from '../src/components/analytics';
import { theme } from '../src/theme';

const RANGE = { startDate: '2026-06-01', endDate: '2026-06-30' };

afterEach(() => cleanup());

describe('AnalyticsFilterRow progressive disclosure', () => {
  it('keeps detailed dates in a popover behind one compact range control', () => {
    const onApply = vi.fn();
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <AnalyticsFilterRow value={RANGE} onApply={onApply} />
      </ThemeProvider>,
    );

    const trigger = screen.getByRole('button', { name: 'Change dates' });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.getAttribute('aria-haspopup')).toBe('dialog');
    expect(screen.queryByLabelText('Start date')).toBeNull();

    fireEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    const popover = within(screen.getByRole('dialog', { name: 'Show messages from' }));
    expect((popover.getByLabelText('Start date') as HTMLInputElement).type).toBe('date');
    expect((popover.getByLabelText('End date') as HTMLInputElement).type).toBe('date');
    expect(popover.getByRole('button', { name: 'Last 30 days' })).toBeTruthy();
    expect(popover.getByRole('button', { name: 'Last 90 days' })).toBeTruthy();

    fireEvent.click(popover.getByRole('button', { name: 'All time' }));
    expect(onApply).toHaveBeenCalledWith({ startDate: '', endDate: '' });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
  });

  it('applies custom dates from the popover form and discards a cancelled draft', () => {
    const onApply = vi.fn();
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <AnalyticsFilterRow value={RANGE} onApply={onApply} />
      </ThemeProvider>,
    );

    const trigger = screen.getByRole('button', { name: 'Change dates' });
    fireEvent.click(trigger);
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-05-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onApply).not.toHaveBeenCalled();

    fireEvent.click(trigger);
    expect((screen.getByLabelText('Start date') as HTMLInputElement).value).toBe('2026-06-01');
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2026-06-15' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(onApply).toHaveBeenCalledWith({ startDate: '2026-06-01', endDate: '2026-06-15' });
  });
});
