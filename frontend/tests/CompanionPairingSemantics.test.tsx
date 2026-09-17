import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CompanionPairingControls } from '../src/components/CompanionPairingControls';
import type { CompanionPairingApi, CompanionPairingStatus } from '../src/services/companionPairingApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const pin: CompanionPairingStatus = {
  pairing_id: 'A'.repeat(43),
  creator_account_id: 'creator-1',
  generation: 1,
  version: 4,
  state: 'admitted',
  expires_at: '2026-09-17T12:05:00Z',
  comparison_code: null,
  agent_identity_thumbprint: 'B'.repeat(43),
};

const api: CompanionPairingApi = {
  pins: vi.fn(async () => [pin]),
  open: vi.fn(async () => ({ ...pin, state: 'open', version: 0, agent_identity_thumbprint: null })),
  get: vi.fn(async () => pin),
  change: vi.fn(async () => pin),
  revoke: vi.fn(async () => ({ ...pin, state: 'revoked' })),
};

beforeEach(() => {
  bridgeTransportStore.reset();
  bridgeTransportStore.bindAccount('creator-1');
  useUserStore.getState().actions.setUserRole('operator');
});

afterEach(() => {
  cleanup();
  bridgeTransportStore.reset();
  useUserStore.getState().actions.setUserRole(null);
  vi.clearAllMocks();
});

describe('CompanionPairingControls connection semantics', () => {
  it('separates a durable linked extension from interrupted current liveness', async () => {
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <CompanionPairingControls api={api} />
      </ThemeProvider>,
    );

    expect(await screen.findByText('Extension linked to this app')).toBeTruthy();
    expect(screen.getByText('Connection interrupted')).toBeTruthy();
    expect(screen.queryByText('Connected to this app')).toBeNull();
    expect(screen.queryByText('Not connected')).toBeNull();
    expect(screen.getByRole('button', { name: 'Disconnect browser extension 1' })).toBeTruthy();
  });
});
