import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { StatusLine } from '../src/components/ui/ReservedRegion';
import { theme } from '../src/theme';

afterEach(cleanup);
const fullText = 'Settings could not be updated. Your existing selection may still be active. Check status before trying again.';
const view = (text: string | null) => <ThemeProvider theme={theme}>
  <StatusLine id="feedback" text={text} summary="Needs attention" detailsOnly tone="error" />
</ThemeProvider>;

describe('StatusLine', () => {
  it('preserves inline text by default without requiring a details dialog', () => {
    render(<ThemeProvider theme={theme}><StatusLine id="feedback" text="Checking settings…" /></ThemeProvider>);
    expect(screen.getByRole('status').textContent).toBe('Checking settings…');
    expect(screen.queryByRole('button', { name: 'Show details' })).toBeNull();
  });

  it('shows the compact summary before measurement and retains the complete error in details', async () => {
    render(view(fullText));
    const region = document.querySelector('[data-reserved-region="feedback"]');
    expect(region).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toBe('Needs attention');
    expect(screen.queryByText(fullText)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
    const dialog = await screen.findByRole('dialog', { name: 'Details' });
    expect(within(dialog).getByRole('alert').textContent).toBe(fullText);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Details' })).toBeNull());
    expect(document.querySelector('[data-reserved-region="feedback"]')).toBe(region);
  });

  it('closes details when the original text changes or becomes unavailable', async () => {
    const rendered = render(view(fullText));
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
    await screen.findByRole('dialog', { name: 'Details' });
    rendered.rerender(view('A different current status.'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Details' })).toBeNull());
    expect(screen.queryByText(fullText)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
    const dialog = await screen.findByRole('dialog', { name: 'Details' });
    expect(within(dialog).getByText('A different current status.')).toBeTruthy();
    rendered.rerender(view(null));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Details' })).toBeNull());
    expect(screen.queryByRole('button', { name: 'Show details' })).toBeNull();
    expect(screen.queryByText('A different current status.')).toBeNull();
  });

  it('keeps essential text inline even when compact details are requested', () => {
    render(<ThemeProvider theme={theme}><StatusLine id="feedback" text={fullText} essential detailsOnly tone="error" /></ThemeProvider>);
    expect(screen.getByRole('alert').textContent).toBe(fullText);
    expect(screen.queryByRole('button', { name: 'Show details' })).toBeNull();
  });
});
