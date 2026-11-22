import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it } from 'vitest';
import { SetupPrompt } from '../src/components/SetupPrompt';
import { theme } from '../src/theme';

afterEach(cleanup);
it('shows the dashboard setup prompt with the browser connection handoff', () => {
  render(<ThemeProvider theme={theme}><MemoryRouter><SetupPrompt title="Set up your browser to see your message counts" compact /></MemoryRouter></ThemeProvider>);
  expect(screen.getByRole('heading', { name: 'Set up your browser to see your message counts' })).toBeTruthy();
  expect(screen.getByText('New messages appear in this app once the extension is connected. Full analytics is optional.')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Continue in browser' }).getAttribute('href')).toBe('/settings#browser-extension');
  expect(screen.queryByRole('list')).toBeNull();
});

it('explains the destination beside the setup link', () => {
  render(<ThemeProvider theme={theme}><MemoryRouter><SetupPrompt title="Finish setup" /></MemoryRouter></ThemeProvider>);
  expect(screen.getByText('Open Browser extension in Settings.')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Continue setup' }).getAttribute('href')).toBe('/settings#browser-extension');
});

it('skips the completed connection and keeps history out of required setup', () => {
  render(<ThemeProvider theme={theme}><MemoryRouter><SetupPrompt extensionConnected title="Finish setup" /></MemoryRouter></ThemeProvider>);
  expect(screen.getByRole('link', { name: 'Continue setup' }).getAttribute('href')).toBe('/settings#full-analytics');
  expect(screen.getAllByRole('listitem')).toHaveLength(1);
  expect(screen.getByRole('listitem').getAttribute('data-step-state')).toBe('done');
  expect(screen.queryByText('Turn on message history')).toBeNull();
});
