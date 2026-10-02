import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { FreshnessStatus } from '../src/components/ui/FreshnessStatus';
import type { ExtensionPortState } from '../src/services/extensionPort';
import { theme } from '../src/theme';

const port = vi.hoisted(() => ({
  subscribe: vi.fn(), getState: vi.fn(), open: vi.fn(),
}));
vi.mock('../src/services/extensionPort', () => ({ defaultExtensionPort: () => port }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it.each(['setup', 'creator'] as const)('opens %s only after a navigation click and releases its port', (destination) => {
  let state = { status: 'connecting' } as ExtensionPortState;
  let notify = () => {};
  const unsubscribe = vi.fn();
  port.getState.mockImplementation(() => state);
  port.subscribe.mockImplementation((listener: () => void) => { notify = listener; return unsubscribe; });
  render(<ThemeProvider theme={theme}><FreshnessStatus freshness={null} bridge="connected" snapshotUsable
    override={{ label: 'Paused', title: 'Paused', sentence: '', icon: 'pause', tone: 'user',
      action: { label: 'Open extension', destination } }} /></ThemeProvider>);
  expect(port.subscribe).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: /Status:/ }));
  expect(port.subscribe).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Open extension' }));
  expect(port.subscribe).toHaveBeenCalledTimes(1);
  expect(port.open).not.toHaveBeenCalled();
  act(() => { state = { status: 'connected' } as ExtensionPortState; notify(); notify(); });
  expect(port.open).toHaveBeenCalledExactlyOnceWith(destination);
  expect(unsubscribe).toHaveBeenCalledTimes(1);
});
