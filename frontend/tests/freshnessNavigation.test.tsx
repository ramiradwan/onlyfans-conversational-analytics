import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { FreshnessStatus } from '../src/components/ui/FreshnessStatus';
import { theme } from '../src/theme';
import { desktopFixture } from './desktopOnboardingFixture';

const api = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock('../src/services/browserControlApi', () => ({ browserControlApi: api }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it.each(['setup', 'creator'] as const)('opens %s through Brain only after a navigation click', async destination => {
  api.open.mockResolvedValue('delivered');
  const onboarding = desktopFixture().api;
  const view = render(<ThemeProvider theme={theme}><FreshnessStatus onboarding={onboarding} freshness={null} bridge="connected" snapshotUsable
    override={{ label: 'Paused', title: 'Paused', sentence: '', icon: 'pause', tone: 'user',
      action: { label: 'Open extension', destination } }} /></ThemeProvider>);
  expect(api.open).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: /Status:/ }));
  expect(api.open).not.toHaveBeenCalled();
  expect(onboarding.begin).not.toHaveBeenCalled();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Open extension' })));
  if (destination === 'setup') {
    expect(onboarding.begin).toHaveBeenCalledExactlyOnceWith('browser.access');
    expect(api.open).not.toHaveBeenCalled();
  } else {
    expect(onboarding.begin).not.toHaveBeenCalled();
    expect(api.open).toHaveBeenCalledExactlyOnceWith(destination, expect.any(AbortSignal));
    const signal = api.open.mock.calls[0][1] as AbortSignal;
    view.unmount(); expect(signal.aborted).toBe(true);
  }
});

it('keeps a failed browser action visible with a next step', async () => {
  const onboarding = desktopFixture().api;
  vi.mocked(onboarding.begin).mockRejectedValue(Error('task_unavailable'));
  render(<ThemeProvider theme={theme}><FreshnessStatus onboarding={onboarding} freshness={null} bridge="connected" snapshotUsable
    override={{ label: 'Paused', title: 'Paused', sentence: '', icon: 'pause', tone: 'user',
      action: { label: 'Open extension', destination: 'setup' } }} /></ThemeProvider>);
  fireEvent.click(screen.getByRole('button', { name: /Status:/ }));
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Open extension' })));
  expect(screen.getByRole('alert').textContent).toMatch(/could not be opened/);
});
