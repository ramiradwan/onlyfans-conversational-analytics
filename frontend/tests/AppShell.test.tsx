import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  MemoryRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom';

import { ThemeToggle } from '../src/components/ThemeToggle';
import { AppDrawer } from '../src/layouts/AppDrawer';
import { AppShell } from '../src/layouts/AppShell';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

function setRole(role: 'creator-ceo' | 'operator') {
  useUserStore.getState().actions.setUserRole(role);
}

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function renderDrawer({
  initialEntry = '/',
  mobileOpen = true,
}: {
  initialEntry?: string;
  mobileOpen?: boolean;
} = {}) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      <MemoryRouter initialEntries={[initialEntry]}>
        <AppDrawer
          drawerWidth={72}
          mobileDrawerWidth={264}
          mobileOpen={mobileOpen}
          onDrawerClose={() => undefined}
        />
        <LocationProbe />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

function renderShell(initialEntry: string) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<h1>Dashboard workspace</h1>} />
            <Route path="inbox" element={<h1>Inbox workspace</h1>} />
            <Route path="analytics" element={<h1>Analytics workspace</h1>} />
            <Route path="graph-explorer" element={<h1>Graph workspace</h1>} />
            <Route path="*" element={<Navigate replace to="/" />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

beforeEach(() => setRole('creator-ceo'));
afterEach(() => cleanup());

describe('application shell routing and navigation', () => {
  it('renders the URL-selected route inside the production shell', () => {
    renderShell('/analytics');

    expect(screen.getByRole('main')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Analytics workspace' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Dashboard workspace' })).toBeNull();
    const activeAnalyticsLinks = screen
      .getAllByRole('link', { name: 'Analytics' })
      .filter((link) => link.classList.contains('active'));
    expect(activeAnalyticsLinks.length).toBeGreaterThan(0);
  });

  it('shows creator route labels and uses real links for desktop and mobile navigation', () => {
    renderDrawer({ initialEntry: '/analytics' });

    const desktopNavigation = screen.getByLabelText('Desktop navigation');
    const mobileNavigation = screen.getByLabelText('Mobile navigation');
    expect(
      within(desktopNavigation)
        .getByRole('link', { name: 'Dashboard', hidden: true })
        .getAttribute('href'),
    ).toBe('/');
    expect(
      within(desktopNavigation)
        .getByRole('link', { name: 'Inbox', hidden: true })
        .getAttribute('href'),
    ).toBe('/inbox');
    expect(
      within(desktopNavigation)
        .getByRole('link', { name: 'Analytics', hidden: true })
        .getAttribute('href'),
    ).toBe('/analytics');
    expect(
      within(desktopNavigation)
        .getByRole('link', { name: 'Graph Explorer', hidden: true })
        .getAttribute('href'),
    ).toBe('/graph-explorer');
    expect(within(mobileNavigation).getByRole('link', { name: 'Dashboard' })).toBeTruthy();
    expect(within(mobileNavigation).getByRole('link', { name: 'Inbox' })).toBeTruthy();
    expect(within(mobileNavigation).getByRole('link', { name: 'Analytics' })).toBeTruthy();
    expect(within(mobileNavigation).getByRole('link', { name: 'Graph Explorer' })).toBeTruthy();

    fireEvent.click(within(mobileNavigation).getByRole('link', { name: 'Inbox' }));
    expect(screen.getByLabelText('Current route').textContent).toBe('/inbox');
  });

  it('limits operator navigation to the Inbox role label', () => {
    setRole('operator');
    renderDrawer();

    const desktopNavigation = screen.getByLabelText('Desktop navigation');
    const mobileNavigation = screen.getByLabelText('Mobile navigation');
    for (const navigation of [desktopNavigation, mobileNavigation]) {
      expect(
        within(navigation).getByRole('link', { name: 'Inbox', hidden: true }),
      ).toBeTruthy();
      expect(
        within(navigation).queryByRole('link', { name: 'Dashboard', hidden: true }),
      ).toBeNull();
      expect(
        within(navigation).queryByRole('link', { name: 'Analytics', hidden: true }),
      ).toBeNull();
      expect(
        within(navigation).queryByRole('link', { name: 'Graph Explorer', hidden: true }),
      ).toBeNull();
    }
  });

  it('opens the labelled mobile navigation from the shell menu button', () => {
    renderShell('/inbox');

    const mobileNavigation = screen.getByLabelText('Mobile navigation');
    expect(
      within(mobileNavigation).queryByRole('link', { name: 'Inbox' }),
    ).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Open navigation' }));

    expect(
      within(screen.getByLabelText('Mobile navigation')).getByRole('link', { name: 'Inbox' }),
    ).toBeTruthy();
  });
});

describe('ThemeToggle', () => {
  it('cycles light, dark, and system modes through accessible labels', async () => {
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <ThemeToggle />
      </ThemeProvider>,
    );

    const darkButton = await screen.findByRole('button', { name: 'Switch to dark mode' });
    fireEvent.click(darkButton);

    const systemButton = await screen.findByRole('button', { name: 'Switch to system mode' });
    fireEvent.click(systemButton);

    const lightButton = await screen.findByRole('button', { name: 'Switch to light mode' });
    fireEvent.click(lightButton);

    expect(await screen.findByRole('button', { name: 'Switch to dark mode' })).toBeTruthy();
  });
});
