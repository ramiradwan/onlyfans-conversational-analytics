import { describe, expect, it, vi } from 'vitest';

import { createTextAnalysisReissueApi, parseReissueStatus } from '../src/services/textAnalysisReissueApi';

const FLOW = '10000000-0000-4000-8000-000000000001';
const awaiting = { current: { flow_id: FLOW, state: 'awaiting_authorization', navigation_available: true } };
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });

describe('Local licence recovery API', () => {
  it('uses inert status and empty authenticated actions with only opaque local handles', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(awaiting));
    const api = createTextAnalysisReissueApi({ fetcher, csrf: () => 'local-csrf' });
    expect(await api.status()).toEqual(awaiting);
    expect(fetcher.mock.calls[0]).toEqual(['/api/v1/nlp/license-reissue', expect.objectContaining({ method: 'GET', body: undefined, credentials: 'same-origin', cache: 'no-store', redirect: 'error' })]);
    fetcher.mockResolvedValueOnce(json(awaiting));
    expect(await api.begin()).toEqual(awaiting);
    fetcher.mockResolvedValueOnce(json(awaiting));
    expect(await api.resume(FLOW)).toEqual(awaiting);
    for (const [, init] of fetcher.mock.calls.slice(1)) {
      expect(init).toMatchObject({ method: 'POST', body: '{}', headers: { 'X-CSRF-Token': 'local-csrf', 'Content-Type': 'application/json' } });
    }
  });

  it('passes only a bounded one-use code to the fixed native command', async () => {
    const openEntry = vi.fn<(_: string) => Promise<void>>().mockResolvedValue(undefined);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ entry_code: 'A'.repeat(43) }));
    const api = createTextAnalysisReissueApi({ fetcher, csrf: () => 'local-csrf', openEntry });
    await api.openBrowser(FLOW);
    expect(fetcher.mock.calls[0][0]).toBe(`/api/v1/nlp/license-reissue/${FLOW}/browser-entry`);
    expect(openEntry).toHaveBeenCalledExactlyOnceWith('A'.repeat(43));
    expect(JSON.stringify(fetcher.mock.calls)).not.toContain('entry_reference');
  });

  it('does not open a browser after the calling session is abandoned', async () => {
    const controller = new AbortController(), openEntry = vi.fn<(_: string) => Promise<void>>();
    const fetcher = vi.fn<typeof fetch>(async () => { controller.abort(); return json({ entry_code: 'A'.repeat(43) }); });
    await createTextAnalysisReissueApi({ fetcher, csrf: () => 'local-csrf', openEntry }).openBrowser(FLOW, controller.signal);
    expect(openEntry).not.toHaveBeenCalled();
  });

  it.each([
    { current: { ...awaiting.current, license: 'signed' } },
    { current: { ...awaiting.current, state: 'active' } },
    { current: { ...awaiting.current, flow_id: 'hosted-operation' } },
    { current: { ...awaiting.current, state: 'adopted' } },
    { current: null, confirmation: {} },
    { current: { ...awaiting.current, navigation_available: 1 } },
  ])('refuses unsupported or protected status fields: %j', value => {
    expect(() => parseReissueStatus(value)).toThrow();
  });

  it.each(['https://host.invalid/', 'a'.repeat(42), 'a'.repeat(44)])('refuses malformed native entry %s', async entry_code => {
    const openEntry = vi.fn<(_: string) => Promise<void>>();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ entry_code }));
    await expect(createTextAnalysisReissueApi({ fetcher, csrf: () => 'local-csrf', openEntry }).openBrowser(FLOW)).rejects.toThrow();
    expect(openEntry).not.toHaveBeenCalled();
  });

  it('refuses a response for a different retained local request', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ current: { ...awaiting.current, flow_id: '20000000-0000-4000-8000-000000000001' } }));
    await expect(createTextAnalysisReissueApi({ fetcher, csrf: () => 'local-csrf' }).resume(FLOW)).rejects.toThrow();
  });

  it('bounds streamed status before JSON parsing', async () => {
    const cancel = vi.fn();
    const stream = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(4097)); }, cancel });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(stream, { headers: { 'Content-Type': 'application/json' } }));
    await expect(createTextAnalysisReissueApi({ fetcher }).status()).rejects.toThrow();
    expect(cancel).toHaveBeenCalledOnce();
  });

  it('refuses an action without CSRF and never displays hosted refusal bodies', async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(createTextAnalysisReissueApi({ fetcher, csrf: () => null }).begin()).rejects.toMatchObject({ code: 'unauthorized' });
    expect(fetcher).not.toHaveBeenCalled();
    fetcher.mockResolvedValue(new Response('private provider material', { status: 503 }));
    await expect(createTextAnalysisReissueApi({ fetcher }).status()).rejects.toMatchObject({ code: 'unavailable', message: 'Licence recovery could not be verified.' });
  });
});
