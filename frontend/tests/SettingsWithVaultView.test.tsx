import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HistorySettings } from '../src/protocol';
import type { CapabilityLicenseApi, CapabilityLicenseReadiness } from '../src/services/capabilityLicenseApi';
import type { CompanionPairingApi, CompanionPairingStatus } from '../src/services/companionPairingApi';
import type { CreatorVaultApi, CreatorVaultStatus } from '../src/services/creatorVaultApi';
import type { HistorySettingsApi } from '../src/services/historySettingsApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';
import SettingsWithVaultView from '../src/views/SettingsWithVaultView';

type Section = 'pairing' | 'history' | 'activation' | 'vault';

const unused = async () => { throw new Error('not used'); };

const history: HistorySettings = {
  creator_account_id: 'creator-1',
  settings_revision: 1,
  consent_policy_version: 'history-consent-v1',
  consent_revision: null,
  authorized_platform_creator_id: null,
  desired_state: 'not_started',
  effective_state: 'not_applied',
  effective_config_revision: null,
  recent_window_days: 30,
  page_size: 50,
  pages_per_wake: 2,
  request_interval_ms: 1000,
  retry_limit: 3,
  updated_at: '2026-07-19T12:00:00Z',
};

const readiness: CapabilityLicenseReadiness = {
  schema: 'ofca-analysis-readiness/v1',
  commercial_authority: 'required',
  analysis_admission: 'blocked',
};

const vaultStatus: CreatorVaultStatus = {
  creator_account_id: 'creator-1',
  policy: { enabled: false, policy_type: 'disabled', finite_horizon_days: null, revision: 0 },
  capabilities: {
    finite_retention: true,
    indefinite_retention: false,
    deletion_scopes: ['all'],
    unlink_archive_treatments: ['preserve'],
    export: true,
  },
};

/** Resolves every section's first request at once, except `slow`, which resolves on `release`. */
function apis(slow: Section) {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const answer = <T,>(section: Section, value: T) => async () => {
    if (section === slow) await gate;
    return value;
  };
  const pairingApi: CompanionPairingApi = {
    pins: vi.fn(answer<CompanionPairingStatus[]>('pairing', [])),
    revoke: vi.fn(unused),
    open: vi.fn(unused),
    get: vi.fn(unused),
    change: vi.fn(unused),
  };
  const historyApi: HistorySettingsApi = {
    get: vi.fn(answer('history', history)),
    update: vi.fn(unused),
    revoke: vi.fn(unused),
  };
  const activationApi: CapabilityLicenseApi = {
    readiness: vi.fn(answer('activation', readiness)),
    redeem: vi.fn(unused),
  };
  const vaultApi: CreatorVaultApi = {
    get: vi.fn(answer('vault', vaultStatus)),
    command: vi.fn(unused),
    exportDocument: vi.fn(unused),
  };
  return { activationApi, historyApi, pairingApi, release, vaultApi };
}

beforeEach(() => {
  useUserStore.getState().actions.setUserRole('creator-ceo');
  bridgeTransportStore.bindAccount('creator-1');
});

afterEach(() => {
  cleanup();
  useUserStore.getState().actions.setUserRole(null);
  bridgeTransportStore.reset();
});

describe('SettingsWithVaultView', () => {
  it.each<Section>(['pairing', 'history', 'activation', 'vault'])(
    'waits for the %s section before showing any section',
    async (slow) => {
      const { release, ...props } = apis(slow);
      render(
        <ThemeProvider theme={theme} defaultMode="light">
          <SettingsWithVaultView {...props} />
        </ThemeProvider>,
      );
      await act(async () => undefined);

      expect(screen.getByRole('status').textContent).toBe('Processing your data…');
      expect(screen.queryByRole('heading', { name: 'Browser extension' })).toBeNull();
      expect(screen.queryByRole('heading', { name: 'Stored messages' })).toBeNull();

      await act(async () => release());

      expect(screen.queryByText('Processing your data…')).toBeNull();
      expect(screen.getByRole('button', { name: 'Connect extension' })).toBeTruthy();
      expect(screen.getByRole('heading', { name: 'Message history' })).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Turn on full analytics' })).toBeTruthy();
      expect(screen.getByRole('heading', { name: 'Stored messages' })).toBeTruthy();
    },
  );
});
