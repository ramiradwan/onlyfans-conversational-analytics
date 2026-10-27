import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';

import { describe, expect, it, vi } from 'vitest';

const source = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../app/onboarding/reissue_entry.js'), 'utf8');
const TARGET = 'https://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'.repeat(43);
function fixture(hash = '#code=' + 'B'.repeat(43), response = new Response(JSON.stringify({ url: TARGET }), { headers: { 'Content-Type': 'application/json' } })) {
  const order: string[] = [], status = { textContent: '' };
  const replace = vi.fn(), fetch = vi.fn(async () => { order.push('fetch'); return response; });
  const context = { location: { hash, pathname: '/text-analysis-reissue/entry', replace },
    history: { replaceState: vi.fn(() => order.push('clear')) }, document: { getElementById: () => status },
    URL, URLSearchParams, TextDecoder, Uint8Array, fetch,
    localStorage: { setItem: () => { throw Error('storage is forbidden'); } },
    sessionStorage: { setItem: () => { throw Error('storage is forbidden'); } } };
  return { context, replace, fetch, status, order };
}
describe('Licence recovery browser entry', () => {
  it('clears the fragment before nonauthorizing one-use consumption and fixed hosted navigation', async () => {
    const x = fixture(); await runInNewContext(source, x.context);
    expect(x.order).toEqual(['clear', 'fetch']);
    expect(x.fetch).toHaveBeenCalledExactlyOnceWith('/api/v1/nlp/license-reissue/browser-entry/consume', expect.objectContaining({
      method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store', body: JSON.stringify({ code: 'B'.repeat(43) }),
    }));
    expect(x.replace).toHaveBeenCalledExactlyOnceWith(TARGET);
  });

  it.each(['', '#code=short', '#code=' + 'B'.repeat(43) + '&extra=1'])('refuses malformed fragments without requests: %s', async hash => {
    const x = fixture(hash); await runInNewContext(source, x.context);
    expect(x.fetch).not.toHaveBeenCalled(); expect(x.replace).not.toHaveBeenCalled();
    expect(x.order).toEqual(['clear']); expect(x.status.textContent).toContain('Open a fresh link');
  });

  it.each(['http://example.test/', TARGET + '?secret=value', TARGET + '#secret', 'https://user@example.test/' + 'A'.repeat(43), 'https://example.test/unallocated'])('refuses an unallocated hosted navigation: %s', async url => {
    const x = fixture(undefined, new Response(JSON.stringify({ url }), { headers: { 'Content-Type': 'application/json' } }));
    await runInNewContext(source, x.context);
    expect(x.replace).not.toHaveBeenCalled(); expect(x.status.textContent).toContain('Open a fresh link');
  });

  it('cancels an oversized body before navigation', async () => {
    const cancel = vi.fn();
    const body = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(4097)); }, cancel });
    const x = fixture(undefined, new Response(body, { headers: { 'Content-Type': 'application/json' } }));
    await runInNewContext(source, x.context);
    expect(cancel).toHaveBeenCalledOnce(); expect(x.replace).not.toHaveBeenCalled();
  });
});
