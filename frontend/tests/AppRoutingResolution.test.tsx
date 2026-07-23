import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { AppRouter } from '../src/routing/AppRouter';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function renderApp(initialEntry: string) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      <MemoryRouter initialEntries={[initialEntry]}>
        <AppRouter />
        <LocationProbe />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

beforeEach(() => {
  // Mirrors the pre-mount state: the Brain session hasn't reported a role yet.
  useUserStore.setState({ role: null, roleResolved: false });
  bridgeTransportStore.reset();
});

afterEach(() => cleanup());

describe('gated deep links survive a hard load while the role resolves', () => {
  it('waits for the role instead of redirecting a hard-loaded /inbox to "/"', async () => {
    renderApp('/inbox');

    // Unresolved: no gated route table exists yet, but the router must not have
    // fallen through to the catch-all redirect either.
    expect(screen.getByLabelText('Current route').textContent).toBe('/inbox');
    expect(screen.queryByRole('heading', { name: 'Inbox' })).toBeNull();

    act(() => {
      useUserStore.getState().actions.setUserRole('operator');
    });

    expect(
      await screen.findByRole('heading', { name: 'Inbox' }, { timeout: 10000 }),
    ).toBeTruthy();
    expect(screen.getByLabelText('Current route').textContent).toBe('/inbox');
  });

  it('still redirects away once an unresolved role resolves to one without access', () => {
    renderApp('/inbox');
    expect(screen.getByLabelText('Current route').textContent).toBe('/inbox');

    act(() => {
      useUserStore.getState().actions.setUserRole(null);
    });

    expect(screen.getByLabelText('Current route').textContent).toBe('/');
    expect(screen.queryByRole('heading', { name: 'Inbox' })).toBeNull();
  });
});
