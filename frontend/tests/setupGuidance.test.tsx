import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it } from 'vitest';
import { SetupPrompt } from '../src/components/SetupPrompt';
import { theme } from '../src/theme';

afterEach(cleanup);
it('explains the destination beside the setup link', () => {
  render(<ThemeProvider theme={theme}><MemoryRouter><SetupPrompt title="Finish setup" /></MemoryRouter></ThemeProvider>);
  expect(screen.getByText('In Settings, choose Connect extension under Browser extension.')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Continue setup' }).getAttribute('href')).toBe('/settings#browser-extension');
});
