import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import command from '../../shared/nlp/application/v1/fixtures/enrollment-command.json';
import result from '../../shared/nlp/application/v1/fixtures/enrollment-result.json';
import record from '../../shared/nlp/application/v1/fixtures/client-enrollment-record.json';
import intent from '../../shared/nlp/application/v1/fixtures/client-enrollment-intent.json';
import outcome from '../../shared/nlp/application/v1/fixtures/preview-outcome.json';
import saved from '../../shared/nlp/application/v1/fixtures/preview-saved.json';
import { clientIntent, enrollmentCommand, textAnalysisCanonicalJson } from '../src/analytics/textAnalysisContract';
import { createTextAnalysisApi, TextAnalysisApiError } from '../src/services/textAnalysisApi';

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => vi.unstubAllGlobals());
describe('text analysis HTTP', () => {
  it('normalizes typed mutation bodies and sends only same-origin CSRF requests', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json(record)).mockResolvedValueOnce(json(result));
    const api = createTextAnalysisApi({ fetcher, csrf: () => 'csrf' });
    await api.retain(clientIntent.parse(intent)); await api.command(enrollmentCommand.parse(command));
    for (const [, request] of fetcher.mock.calls) expect(request).toMatchObject({ method: 'POST', credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: { 'X-CSRF-Token': 'csrf', 'Content-Type': 'application/json' } });
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual(command);
  });
  it('classifies only the exact published unknown lookup codes as absence', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json({ detail: { code: 'text_analysis_client_unknown' } }, 404))
      .mockResolvedValueOnce(json({ detail: { code: 'text_analysis_command_unknown' } }, 404))
      .mockResolvedValueOnce(json({ detail: { code: 'text_analysis_preview_unknown' } }, 404))
      .mockResolvedValueOnce(json({ detail: { code: 'different_code' } }, 404));
    const api = createTextAnalysisApi({ fetcher });
    expect(await api.retained('enrollment_command', command.command_key)).toBeNull();
    expect(await api.commandReceipt(command.command_key)).toBeNull();
    expect(await api.outcome(outcome.request.external_key)).toBeNull();
    await expect(api.retained('enrollment_command', command.command_key)).rejects.toMatchObject({ kind: 'unknown', status: 404 });
    expect(fetcher.mock.calls.every(([, request]) => request?.method === 'GET')).toBe(true);
  });
  it.each([401, 403, 409, 503])('never turns HTTP %s into an absent request or renders raw details', async (status) => {
    const api = createTextAnalysisApi({ fetcher: vi.fn(async () => json({ detail: { code: 'text_analysis_client_unknown', private: 'sensitive-value' } }, status)) });
    try { await api.retained('enrollment_command', command.command_key); throw new Error('Expected refusal'); }
    catch (error) { expect(error).toBeInstanceOf(TextAnalysisApiError); expect(String(error)).not.toContain('sensitive-value'); }
  });
  it('does not dispatch after cancellation while CSRF is being obtained', async () => {
    let release!: (value: string) => void;
    const csrf = new Promise<string>((resolve) => { release = resolve; }), controller = new AbortController(), fetcher = vi.fn<typeof fetch>();
    const work = createTextAnalysisApi({ fetcher, csrf: () => csrf }).command(enrollmentCommand.parse(command), controller.signal);
    controller.abort(); release('csrf');
    await expect(work).rejects.toMatchObject({ name: 'AbortError' }); expect(fetcher).not.toHaveBeenCalled();
  });
  it('refuses a changed displayed cutoff even when the old confirmation is otherwise intact', async () => {
    const value = structuredClone(outcome);
    value.scope.cutoff = '2026-10-25T01:00:00Z'; value.scope.lower_exclusive = '2026-07-27T01:00:00Z';
    await expect(createTextAnalysisApi({ fetcher: vi.fn(async () => json(value)) }).outcome(value.request.external_key)).rejects.toThrow();
  });
  it('admits a bounded saved envelope above the worker frame cap without relaxing the output cap', async () => {
    const value = structuredClone(saved), mentions = value.output.summaries.find((item) => item.kind === 'mention_category')!;
    for (const exam of value.output.examination) {
      exam.coverage.source_codepoints = 131072; exam.coverage.examined = [{ start: 0, end: 131072 }];
    }
    const prototype = structuredClone(mentions.evidence[0]);
    function fill(size: number) {
      mentions.evidence = Array.from({ length: size }, (_, index) => ({ ...prototype,
        reference: { ...prototype.reference, span: { start: index * 2, end: index * 2 + 1 } } }));
      mentions.occurrences = size;
    }
    let lower = 1, upper = 4096;
    while (lower < upper) {
      const middle = Math.ceil((lower + upper) / 2); fill(middle);
      if (textAnalysisCanonicalJson(value.output).length <= 1_048_448) lower = middle; else upper = middle - 1;
    }
    fill(lower);
    const wire = textAnalysisCanonicalJson(value);
    expect(wire.length).toBeGreaterThan(1_048_576); expect(textAnalysisCanonicalJson(value.output).length).toBeLessThanOrEqual(1_048_576);
    const api = createTextAnalysisApi({ fetcher: vi.fn(async () => new Response(wire)) });
    expect((await api.saved(value.output.contribution_id)).output.summaries[1].occurrences).toBe(lower);
    const tooLarge = createTextAnalysisApi({ fetcher: vi.fn(async () => new Response(' '.repeat(2_097_153))) });
    await expect(tooLarge.saved(value.output.contribution_id)).rejects.toMatchObject({ kind: 'unknown' });
  });
});
