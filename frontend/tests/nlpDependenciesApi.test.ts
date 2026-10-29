import { describe, expect, it, vi } from 'vitest';

import { createNlpDependenciesApi } from '../src/services/nlpDependenciesApi';

const unavailable = { configured: false, profiles: [], acquisitions: [], available_profiles: [], selected: null, failure: 'nlp_unavailable', resume_granularity: 'verified_files_current_file_restarts' };
describe('nlpDependenciesApi', () => {
  it('accepts rehashed-prefix recovery without exposing partial-file metadata', async () => {
    const current = { ...unavailable, resume_granularity: 'verified_files_and_rehashed_prefix' };
    const request = vi.fn().mockResolvedValue(new Response(JSON.stringify(current)));
    expect(await createNlpDependenciesApi({ fetch: request }).get()).toEqual(current);
  });
  it('reads cached status without CSRF or a mutation', async () => {
    const request = vi.fn().mockResolvedValue(new Response(JSON.stringify(unavailable)));
    const csrf = vi.fn(); const api = createNlpDependenciesApi({ fetch: request, getCsrfToken: csrf });
    expect(await api.get()).toEqual(unavailable);
    expect(request).toHaveBeenCalledWith('/api/v1/nlp/dependencies', expect.objectContaining({ method: 'GET', credentials: 'same-origin' }));
    expect(csrf).not.toHaveBeenCalled();
  });
  it('binds explicit commands to the same-origin session and CSRF token', async () => {
    const request = vi.fn().mockResolvedValue(new Response(JSON.stringify({ operation: null, status: unavailable })));
    const api = createNlpDependenciesApi({ fetch: request, getCsrfToken: () => 'test-token' });
    await api.command({ action: 'account_pause' });
    expect(request).toHaveBeenCalledWith('/api/v1/nlp/dependencies', expect.objectContaining({
      method: 'POST', credentials: 'same-origin', body: '{"action":"account_pause"}',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-token' }),
    }));
  });
  it('does not send a command without CSRF or after cancellation while obtaining it', async () => {
    const request = vi.fn(); const controller = new AbortController();
    await expect(createNlpDependenciesApi({ fetch: request, getCsrfToken: () => null }).command({ action: 'refresh' })).rejects.toThrow();
    const api = createNlpDependenciesApi({ fetch: request, getCsrfToken: async () => { controller.abort(); return 'token'; } });
    await expect(api.command({ action: 'refresh' }, controller.signal)).rejects.toThrow();
    expect(request).not.toHaveBeenCalled();
  });
  it('rejects malformed status and mismatched operation observations', async () => {
    const request = vi.fn().mockResolvedValueOnce(new Response('{}')).mockResolvedValueOnce(new Response(JSON.stringify({ id: 'b'.repeat(32), kind: 'acquire', profile_sha256: null, profile_id: null, revision: null, state: 'running', verified_files: 0, in_flight: true, resume_granularity: unavailable.resume_granularity })));
    const api = createNlpDependenciesApi({ fetch: request });
    await expect(api.get()).rejects.toThrow();
    await expect(api.observe('a'.repeat(32))).rejects.toThrow('Text analysis operation could not be verified.');
  });
  it('refuses malformed costs and never exposes server exception details', async () => {
    const costs = { operation_id: 'a'.repeat(32), profile_sha256: 'a'.repeat(64), declared_full_artifact_download_bytes: 1, data_download_bytes: 1, incremental_data_download_bytes: 1, profile_metadata_bytes: 1, installed_bytes: -1, rollback_bytes: 1, acquisition_temporary_bytes: 1, transition_peak_memory_bytes: 1, inference_threads: 1 };
    const request = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ operation: costs, status: unavailable }))).mockResolvedValueOnce(new Response('private exception details', { status: 409 }));
    const api = createNlpDependenciesApi({ fetch: request, getCsrfToken: () => 'token' });
    await expect(api.command({ action: 'plan', profile_id: 'topics', revision: 'one' })).rejects.toThrow();
    await expect(api.command({ action: 'refresh' })).rejects.not.toThrow('private exception details');
  });
});
