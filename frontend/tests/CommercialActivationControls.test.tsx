import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
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

function mount(api: CapabilityLicenseApi) {
  return render(
    <ThemeProvider theme={theme}>
      <CommercialActivationControls api={api} />
    </ThemeProvider>,
  );
}

async function showRequired(api: CapabilityLicenseApi) {
  mount(api);
  expect(await screen.findByText('Full activation required')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Continue activation' })).toBeTruthy();
}

afterEach(cleanup);

describe('commercial activation controls', () => {
  it('exposes the customer action when canonical readiness requires activation', async () => {
    await showRequired(makeApi());
    expect(screen.getByLabelText('Activation continuation')).toBeTruthy();
    expect(screen.getByText(/verify it before Full readiness changes/)).toBeTruthy();
  });

  it('rejects malformed input before redemption', async () => {
    const api = makeApi();
    await showRequired(api);
    fireEvent.change(screen.getByLabelText('Activation continuation'), { target: { value: 'clr1.short' } });
    fireEvent.click(screen.getByRole('button', { name: 'Continue activation' }));
    expect(api.redeem).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toContain('complete activation continuation');
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

    fireEvent.change(screen.getByLabelText('Activation continuation'), { target: { value: CONTINUATION } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Continue activation' })));

    expect(redeem).toHaveBeenCalledWith(CONTINUATION, expect.any(AbortSignal));
    expect(screen.getByText('Checking activation')).toBeTruthy();
    expect(screen.queryByText('Full mode is ready')).toBeNull();

    await act(async () => finishReadiness(activeAdmitted));
    expect(await screen.findByText('Full mode is ready')).toBeTruthy();
    expect(screen.queryByLabelText('Activation continuation')).toBeNull();
  });

  it('never treats active commercial authority with blocked analysis as Full-ready', async () => {
    const api = makeApi({ readiness: vi.fn(async () => activeBlocked) });
    mount(api);
    expect(await screen.findByText('Full activation active')).toBeTruthy();
    expect(screen.queryByText('Full mode is ready')).toBeNull();
  });

  it('keeps a failed redemption recoverable and re-reads canonical readiness', async () => {
    const readiness = vi.fn()
      .mockResolvedValueOnce(required)
      .mockResolvedValueOnce(required);
    const redeem = vi.fn(async () => {
      throw new CapabilityLicenseApiError(
        'Activation could not be confirmed. Try again; existing activation remains unchanged.',
        503,
      );
    });
    const api = makeApi({ readiness, redeem });
    await showRequired(api);

    fireEvent.change(screen.getByLabelText('Activation continuation'), { target: { value: CONTINUATION } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Continue activation' })));

    expect(readiness).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/existing activation remains unchanged/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Continue activation' })).toBeTruthy();
    expect((screen.getByLabelText('Activation continuation') as HTMLInputElement).value).toBe(CONTINUATION);
  });

  it('resolves an ambiguous submit only from the subsequent canonical readiness read', async () => {
    const readiness = vi.fn()
      .mockResolvedValueOnce(required)
      .mockResolvedValueOnce(activeAdmitted);
    const redeem = vi.fn(async () => {
      throw new CapabilityLicenseApiError('Activation could not be checked. Try again.');
    });
    const api = makeApi({ readiness, redeem });
    await showRequired(api);

    fireEvent.change(screen.getByLabelText('Activation continuation'), { target: { value: CONTINUATION } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Continue activation' })));

    expect(await screen.findByText('Full mode is ready')).toBeTruthy();
    expect(screen.queryByRole('alert', { name: /could not be checked/i })).toBeNull();
  });

  it('does not render protected commercial identifiers', async () => {
    const api = makeApi();
    await showRequired(api);
    const visible = document.body.textContent ?? '';
    expect(visible).not.toMatch(/reference_id|license_id|issuance_id|seat_id|package|organization_id|installation_id|proof|signature|JWS/i);
  });
});
