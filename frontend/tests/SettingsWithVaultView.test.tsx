import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HistorySettings } from '../src/protocol';
import type { CapabilityLicenseApi, CapabilityLicenseReadiness } from '../src/services/capabilityLicenseApi';
import type { CompanionPairingApi, CompanionPairingStatus } from '../src/services/companionPairingApi';
import type { CreatorVaultApi, CreatorVaultStatus } from '../src/services/creatorVaultApi';
import type { HistorySettingsApi } from '../src/services/historySettingsApi';
import type { NlpDependencyStatus } from '../src/services/nlpDependenciesApi';
import { bridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';
import SettingsWithVaultView from '../src/views/SettingsWithVaultView';
import { desktopFixture } from './desktopOnboardingFixture';

type Section = 'pairing' | 'history' | 'activation' | 'vault';

function renderSettings(children: ReactNode) {
  return render(<ThemeProvider theme={theme} defaultMode="light"><MemoryRouter>{children}</MemoryRouter></ThemeProvider>);
}

it('keeps all five section frames present while one request is pending', () => {
  const view = renderSettings(<SettingsWithVaultView {...apis('vault')} />);
  expect(view.container.querySelectorAll('[data-settings-section]')).toHaveLength(5);
});

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

/** Delivers each section's first owner view, except `slow`, which waits for `release`. */
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
    confirmVerified: vi.fn(unused),
  };
  const historyApi: HistorySettingsApi = {
    get: vi.fn(answer('history', history)),
    update: vi.fn(unused),
    revoke: vi.fn(unused),
  };
  const activationApi: CapabilityLicenseApi = {
    redeem: vi.fn(unused),
  };
  const vaultApi: CreatorVaultApi = {
    get: vi.fn(answer('vault', vaultStatus)),
    command: vi.fn(unused),
    exportDocument: vi.fn(unused),
  };
  const desktop = desktopFixture(false);
  desktop.publish({ snapshot: { ...desktop.api.getState().snapshot!, allowed_actions: ['browser.pair', 'activation.review'] } });
  desktop.readiness(readiness, 'creator-1');
  const initialDesktop = desktop.api.getState().snapshot!;
  if (slow === 'activation') desktop.publish({ snapshot: null });
  const releaseSection = () => {
    release();
    if (slow === 'activation') desktop.publish({ snapshot: initialDesktop });
  };
  const nlpApi = {
    get: vi.fn(async (): Promise<NlpDependencyStatus> => ({ configured: false, profiles: [], acquisitions: [], available_profiles: [], selected: null, failure: 'nlp_unavailable', resume_granularity: 'verified_files_current_file_restarts' })),
    observe: vi.fn(unused), command: vi.fn(unused),
  };
  return { activationApi, historyApi, pairingApi, release: releaseSection, vaultApi, onboardingApi: desktop.api, nlpApi };
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
  it('keeps code entry primary without an offer and uses the same offer dialog when one arrives', async () => {
    const { release, ...props } = apis('vault');
    const desktop = desktopFixture();
    desktop.readiness(readiness, 'creator-1');
    const { container } = renderSettings(<SettingsWithVaultView {...props} onboardingApi={desktop.api} />);
    await act(async () => release());
    const section = within(container.querySelector<HTMLElement>('#full-analytics')!);
    const code = section.getByRole('button', { name: 'Use an activation code' });
    expect(code.className).toContain('MuiButton-contained');
    await act(async () => desktop.publish({ snapshot: { ...desktop.api.getState().snapshot!, allowed_actions: ['activation.confirm'],
      decision: { operation_id: '11111111-1111-4111-8111-111111111111', action: 'activate-complimentary', expires_at: '2030-01-01T12:00:00Z',
        offer: { offer_version_id: 'test-offer', description: 'Full conversation analytics', funding: 'complimentary-pilot',
          pilot_credit_cost: 1, capability: 'analysis-run', licensed_major_version: 2, compatible_artifact_family: 'analysis-artifact',
          update_rights: true, fallback_major_versions: [1] } } } }));
    expect(section.queryByRole('button', { name: 'Use an activation code' })).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(section.getByRole('button', { name: 'Review offer' }));
    const offer = within(await screen.findByRole('dialog', { name: 'Activate Full analytics' }));
    expect(offer.getByText('Uses 1 pilot credit')).toBeTruthy();
    expect(offer.getByRole('button', { name: 'Use an activation code' })).toBeTruthy();
    expect(desktop.api.begin).not.toHaveBeenCalled();
    expect(props.activationApi.redeem).not.toHaveBeenCalled();
  });

  it('opens a text analysis link once this installation shows the section', async () => {
    const { release, ...props } = apis('vault');
    let resolve!: (value: NlpDependencyStatus) => void;
    props.nlpApi.get.mockImplementationOnce(() => new Promise<NlpDependencyStatus>((done) => { resolve = done; }));
    const scrolled: string[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function scrollIntoView(this: Element) { scrolled.push(this.id); };
    try {
      const { container } = render(<ThemeProvider theme={theme} defaultMode="light">
        <MemoryRouter initialEntries={['/settings#text-analysis']}><SettingsWithVaultView {...props} /></MemoryRouter>
      </ThemeProvider>);
      await act(async () => release());
      expect(scrolled).toEqual([]);
      await act(async () => resolve({ configured: true, profiles: [], acquisitions: [], available_profiles: [], selected: null,
        failure: null, resume_granularity: 'verified_files_current_file_restarts' }));
      expect(scrolled).toEqual(['text-analysis']);
      expect(document.activeElement).toBe(container.querySelector('#text-analysis'));
    } finally { Element.prototype.scrollIntoView = original; }
  });

  it('keeps the pending connection action and the surrounding Settings sections mounted', async () => {
    const { release, onboardingApi, ...props } = apis('vault');
    const desktop = desktopFixture();
    const snapshot = { ...desktop.api.getState().snapshot!, allowed_actions: ['browser.pair' as const] };
    let finish!: () => void;
    vi.mocked(desktop.api.begin).mockImplementation(() => new Promise<void>(resolve => { finish = resolve; }));
    desktop.publish({ snapshot });
    const { container } = renderSettings(<SettingsWithVaultView {...props} onboardingApi={desktop.api} />);
    await act(async () => { release(); });
    const sections = [...container.querySelectorAll('[data-settings-section]')];
    const action = screen.getByRole('button', { name: 'Connect extension' });
    fireEvent.click(action);
    expect(action).toHaveProperty('disabled', true);
    expect(action.closest('[data-pairing-actions]')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Connect extension' })).toBe(action);
    expect([...container.querySelectorAll('[data-settings-section]')]).toEqual(sections);
    expect(desktop.api.begin).toHaveBeenCalledExactlyOnceWith('browser.pair');
    expect(onboardingApi.begin).not.toHaveBeenCalled();
    await act(async () => finish());
  });

  it.each<Section>(['pairing', 'history', 'activation', 'vault'])(
    'settles the other sections while %s is pending',
    async (slow) => {
      const { release, ...props } = apis(slow);
      renderSettings(<SettingsWithVaultView {...props} />);
      await act(async () => undefined);

      const headings = { pairing: 'Browser extension', history: 'Message history', activation: 'Full analytics', vault: 'Stored messages' };
      for (const [section, name] of Object.entries(headings)) {
        expect(Boolean(screen.queryByRole('heading', { name }))).toBe(section !== slow);
      }
      await act(async () => release());

      expect(screen.queryByText('Loading settings…')).toBeNull();
      expect(screen.getByRole('button', { name: 'Connect extension' })).toBeTruthy();
      expect(screen.getByRole('heading', { name: 'Message history' })).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Review offer' })).toBeTruthy();
      expect(screen.getByRole('heading', { name: 'Stored messages' })).toBeTruthy();
    },
  );
});
