import { describe, expect, it, vi } from 'vitest';

import { createEngineAcquisitionApi, EngineAcquisitionError } from '../src/api/engineAcquisition';

const idle = { operation_id: null, state: 'not_requested', installed: false, in_flight: false,
  cancel_requested: false, deadline_reached: false, progress_percent: null, reason: null, hresult: null };
const quote = { availability: 'available', review_digest: 'a'.repeat(64), engine_package_version: '1.0.42.0',
  expected_complete_package_bytes: 12345, declared_disk: { installed_space_budget_bytes: 4096,
    temporary_space_budget_bytes: 2048, rollback_space_budget_bytes: 0, required_free_space_bytes: 6144 } };
describe('Engine acquisition API', () => {
  it('reads status and explicit review without inventing command arguments', async () => {
    const invoke = vi.fn().mockResolvedValueOnce(idle).mockResolvedValueOnce(quote);
    const api = createEngineAcquisitionApi({ invoke });
    expect(api.supported()).toBe(true);
    expect(await api.status()).toEqual(idle); expect(await api.review()).toEqual(quote);
    expect(invoke.mock.calls).toEqual([['desktop_engine_acquisition_status', undefined], ['desktop_engine_acquisition_review', undefined]]);
  });
  it('starts only a module with its reviewed digest and cancels the exact operation', async () => {
    const current = { ...idle, operation_id: 'b'.repeat(32), in_flight: true, state: 'acquiring' };
    const invoke = vi.fn().mockResolvedValue(current); const api = createEngineAcquisitionApi({ invoke });
    await api.start(quote.review_digest); await api.cancel(current.operation_id);
    expect(invoke.mock.calls).toEqual([
      ['desktop_engine_acquisition_start', { explicitSelection: 'module', reviewDigest: quote.review_digest }],
      ['desktop_engine_acquisition_cancel', { operationId: current.operation_id }],
    ]);
    await expect(api.start('https://example.test')).rejects.toThrow();
    await expect(api.cancel('not-an-operation')).rejects.toThrow(); expect(invoke).toHaveBeenCalledTimes(2);
  });
  it('refuses mismatched cancellation and fabricated progress or ready state', async () => {
    const invoke = vi.fn().mockResolvedValueOnce({ ...idle, operation_id: 'c'.repeat(32) })
      .mockResolvedValueOnce({ ...idle, progress_percent: 50 }).mockResolvedValueOnce({ ...idle, state: 'installed' });
    const api = createEngineAcquisitionApi({ invoke });
    await expect(api.cancel('b'.repeat(32))).rejects.toThrow();
    await expect(api.status()).rejects.toThrow(); await expect(api.status()).rejects.toThrow();
  });
  it.each([
    { ...quote, expected_complete_package_bytes: 0 },
    { ...quote, expected_complete_package_bytes: Number.MAX_SAFE_INTEGER + 1 },
    { ...quote, expected_complete_package_bytes: true },
    { ...quote, uri: 'https://example.test' },
    { ...quote, engine_package_version: '1.02.3.4' },
    { ...quote, engine_package_version: '1.0.65536.0' },
    { ...quote, declared_disk: { ...quote.declared_disk, required_free_space_bytes: 6145 } },
    { ...quote, declared_disk: { ...quote.declared_disk, rollback_space_budget_bytes: undefined } },
    { ...quote, declared_disk: { ...quote.declared_disk, temporary_space_budget_bytes: -1 } },
    { ...quote, declared_disk: { ...quote.declared_disk, installed_space_budget_bytes: Number.MAX_SAFE_INTEGER, required_free_space_bytes: Number.MAX_SAFE_INTEGER } },
  ])('rejects a malformed quote without starting anything', async (bad) => {
    const invoke = vi.fn().mockResolvedValue(bad); const api = createEngineAcquisitionApi({ invoke });
    await expect(api.review()).rejects.toThrow(); expect(invoke).toHaveBeenCalledOnce();
  });
  it('recognizes unavailable quotes and sanitizes native failures', async () => {
    const invoke = vi.fn().mockResolvedValueOnce({ availability: 'unavailable', reason: 'optional_quote_unavailable' })
      .mockRejectedValueOnce('optional_quote_stale').mockRejectedValueOnce('servicing_previous_owner_unsettled')
      .mockRejectedValueOnce('private exception details');
    const api = createEngineAcquisitionApi({ invoke });
    expect(await api.review()).toEqual({ availability: 'unavailable', reason: 'optional_quote_unavailable' });
    await expect(api.start(quote.review_digest)).rejects.toMatchObject({ kind: 'stale_review' });
    await expect(api.start(quote.review_digest)).rejects.toMatchObject({ kind: 'ownership' });
    await expect(api.status()).rejects.not.toThrow('private exception details');
  });
  it('presentation abort does not send native cancellation', async () => {
    let resolve!: (value: unknown) => void;
    const invoke = vi.fn(() => new Promise<unknown>((done) => { resolve = done; }));
    const api = createEngineAcquisitionApi({ invoke }); const controller = new AbortController();
    const pending = api.start(quote.review_digest, controller.signal); controller.abort(); resolve(idle);
    await expect(pending).rejects.toThrow(); expect(invoke).toHaveBeenCalledOnce();
    await expect(api.review(controller.signal)).rejects.toThrow(); expect(invoke).toHaveBeenCalledOnce();
  });
  it('exposes only a bounded failure category', () => {
    expect(new EngineAcquisitionError('ownership').message).toBe('Engine setup could not be verified.');
  });
});
