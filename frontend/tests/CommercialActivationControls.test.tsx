import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CommercialActivationControls } from '../src/components/CommercialActivationControls';
import {
  CapabilityLicenseApiError,
  type CapabilityLicenseApi,
  type CapabilityLicenseReadiness,
} from '../src/services/capabilityLicenseApi';
import { theme } from '../src/theme';

const CONTINUATION = `clr1.${'A'.repeat(43)}`;
const required: CapabilityLicenseReadiness = {
  schema: 'ofca-analysis-readiness/v1',
  commercial_authority: 'required',
  analysis_admission: 'blocked',
};
const activeAdmitted: CapabilityLicenseReadiness = {
  schema: 'ofca-analysis-readiness/v1',
  commercial_authority: 'active',
  analysis_admission: 'admitted',
};
const activeBlocked: CapabilityLicenseReadiness = {
  schema: 'ofca-analysis-readiness/v1',
  commercial_authority: 'active',
  analysis_admission: 'blocked',
};

function makeApi(overrides: Partial<CapabilityLicenseApi> = {}): CapabilityLicenseApi {
  return {
    readiness: vi.fn(async () => required),
    redeem: vi.fn(async () => ({ state: 'checking' as const })),
    ...overrides,
  };
}

function mount(api: CapabilityLicenseApi, secureSetupUrl = '') {
  return render(
    <ThemeProvider theme={theme}>
      <CommercialActivationControls api={api} secureSetupUrl={secureSetupUrl} />
    </ThemeProvider>,
  );
}

async function showRequired(api: CapabilityLicenseApi, secureSetupUrl = '') {
  mount(api, secureSetupUrl);
  const start = await screen.findByRole('button', { name: 'Turn on full analytics' });
  expect(screen.queryByLabelText('Activation code')).toBeNull();
  expect(start.getAttribute('aria-haspopup')).toBe('dialog');
  fireEvent.click(start);
  expect(screen.getByRole('dialog', { name: 'Turn on full analytics' })).toBeTruthy();
  expect(screen.getByLabelText('Activation code')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Activate' })).toBeTruthy();
}

afterEach(cleanup);

describe('commercial activation controls', () => {
  it('exposes the customer action when canonical readiness requires activation', async () => {
    await showRequired(makeApi(), 'https://setup.example/onboarding');
    expect(screen.getByText('Open secure setup and choose Activate Full.')).toBeTruthy();
    expect(screen.getByText(/Paste the code here/)).toBeTruthy();
    const link = screen.getByRole('link', { name: 'Open secure setup' });
    expect(link.getAttribute('href')).toBe('https://setup.example/onboarding');
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('omits the secure setup link when the release has no setup URL', async () => {
    await showRequired(makeApi());
    expect(screen.queryByRole('link', { name: 'Open secure setup' })).toBeNull();
  });

  it('rejects malformed input before redemption', async () => {
    const api = makeApi();
    await showRequired(api);
    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: 'clr1.short' } });
    fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
    expect(api.redeem).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toContain('Enter the full activation code');
  });

  it('submits the opaque continuation, stays checking after POST, then follows canonical Full readiness', async () => {
    let finishReadiness!: (value: CapabilityLicenseReadiness) => void;
    const readiness = vi.fn()
      .mockResolvedValueOnce(required)
      .mockImplementationOnce(() => new Promise<CapabilityLicenseReadiness>((resolve) => {
        finishReadiness = resolve;
      }));
    const redeem = vi.fn(async () => ({ state: 'checking' as const }));
    const api = makeApi({ readiness, redeem });
    await showRequired(api);

    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: CONTINUATION } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Activate' })));

    expect(redeem).toHaveBeenCalledWith(CONTINUATION, expect.any(AbortSignal));
    expect(screen.getByText('Checking activation…')).toBeTruthy();
    expect(screen.queryByText('On')).toBeNull();

    await act(async () => finishReadiness(activeAdmitted));
    expect(await screen.findByText('On')).toBeTruthy();
    await waitFor(() => expect(screen.queryByLabelText('Activation code')).toBeNull());
  });

  it('never treats active commercial authority with blocked analysis as Full-ready', async () => {
    const api = makeApi({ readiness: vi.fn(async () => activeBlocked) });
    mount(api);
    expect(await screen.findByText("New messages aren't being analyzed")).toBeTruthy();
    expect(screen.getByText('Needs attention')).toBeTruthy();
    expect(screen.queryByText('On')).toBeNull();
  });

  it('keeps a failed redemption recoverable and re-reads canonical readiness', async () => {
    const readiness = vi.fn()
      .mockResolvedValueOnce(required)
      .mockResolvedValueOnce(required);
    const redeem = vi.fn(async () => {
      throw new CapabilityLicenseApiError(
        "Activation couldn't be confirmed right now. Nothing has changed. Try again in a moment.",
        503,
      );
    });
    const api = makeApi({ readiness, redeem });
    await showRequired(api);

    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: CONTINUATION } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Activate' })));

    expect(readiness).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/Nothing has changed. Try again in a moment/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Activate' })).toBeTruthy();
    expect((screen.getByLabelText('Activation code') as HTMLInputElement).value).toBe(CONTINUATION);
  });

  it('resolves an ambiguous submit only from the subsequent canonical readiness read', async () => {
    const readiness = vi.fn()
      .mockResolvedValueOnce(required)
      .mockResolvedValueOnce(activeAdmitted);
    const redeem = vi.fn(async () => {
      throw new CapabilityLicenseApiError("Activation couldn't be checked. Try again.");
    });
    const api = makeApi({ readiness, redeem });
    await showRequired(api);

    fireEvent.change(screen.getByLabelText('Activation code'), { target: { value: CONTINUATION } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Activate' })));

    expect(await screen.findByText('On')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('does not render protected commercial identifiers', async () => {
    const api = makeApi();
    await showRequired(api);
    const visible = document.body.textContent ?? '';
    expect(visible).not.toMatch(/reference_id|license_id|issuance_id|seat_id|package|organization_id|installation_id|proof|signature|JWS/i);
  });
});
