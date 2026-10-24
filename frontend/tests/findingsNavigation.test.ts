import { webcrypto } from 'node:crypto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { buildFindingsUrl, createFindingsClient, type FindingsClient } from '../src/analytics/findingsClient';
import { navigationCanonicalJson, parseFindings, parseGroupedFindings, type FindingsResponseV4 } from '../src/analytics/findingsContract';
import { createFindingsStore, type FindingsStore } from '../src/store/findingsStore';
import { findingsBaselineFixture as baseline, findingsRequest, findingsSource, groupedFindingsResponse } from '../src/story-only/findingsFixtures';
import { context } from './questionFixture';

const request = { ...findingsRequest, view: 'grouped' as const };
let store: FindingsStore | undefined;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { store?.actions.dispose(); store = undefined; vi.unstubAllGlobals(); });
async function rehash(value: FindingsResponseV4) {
  const declaration = value.navigation.declaration!;
  value.navigation.policy = { policy_id: declaration.policy_id, revision: declaration.revision,
    sha256: Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(navigationCanonicalJson(declaration)))),
      (byte) => byte.toString(16).padStart(2, '0')).join('') };
}

describe('declared findings navigation', () => {
  it('uses sorted ASCII JSON including non-ASCII and surrogate-pair escapes', () => {
    expect(navigationCanonicalJson({ z: 'café 😀\u007f', a: ['x', '\n'] }))
      .toBe('{"a":["x","\\n"],"z":"caf\\u00e9 \\ud83d\\ude00\\u007f"}');
  });
  it('verifies an explicit grouped view without summing overlapping child support', async () => {
    const value = await groupedFindingsResponse(request);
    const result = await parseGroupedFindings(value, request);
    expect(result.navigation.current[0].supporting_messages).toBe(1);
    expect(result.tasks[0].rows.reduce((total, row) => total + row.supporting_messages, 0)).toBe(2);
    expect(result.navigation.previous?.[0].share_of_complete_messages).toBeNull();
    expect(() => parseFindings(value, request)).toThrow();
    await expect(parseGroupedFindings(value, findingsRequest)).rejects.toThrow();
  });
  it.each([
    ['hash', (value: FindingsResponseV4) => { value.navigation.policy!.sha256 = 'f'.repeat(64); }],
    ['row order', (value: FindingsResponseV4) => { value.navigation.current.reverse(); }],
    ['union above child sum', (value: FindingsResponseV4) => { value.navigation.current[0].supporting_messages = 3; value.navigation.current[0].complete_supporting_messages = 3; value.navigation.current[0].share_of_complete_messages = 1; }],
    ['union below member support', (value: FindingsResponseV4) => { value.navigation.current[0].supporting_messages = 0; value.navigation.current[0].complete_supporting_messages = 0; value.navigation.current[0].supporting_conversations = 0; value.navigation.current[0].share_of_complete_messages = 0; }],
    ['denominator', (value: FindingsResponseV4) => { value.navigation.current[0].share_of_complete_messages = 1; }],
    ['missing previous', (value: FindingsResponseV4) => { value.navigation.previous = null; }],
    ['frozen label digest', (value: FindingsResponseV4) => { value.tasks[0].rows[0].label_identity_sha256 = `sha256:${'f'.repeat(64)}`; }],
    ['label text drift', (value: FindingsResponseV4) => { value.tasks[0].rows[0].display_label = 'Changed label'; }],
    ['category echo', (value: FindingsResponseV4) => { value.category_filter = { task: 'message_topics', category_id: 'synthetic-topic' }; }],
    ['unknown field', (value: FindingsResponseV4) => { Object.assign(value.navigation, { extra: true }); }],
  ])('rejects %s drift', async (_, mutate) => {
    const value = await groupedFindingsResponse(request); mutate(value);
    await expect(parseGroupedFindings(value, request)).rejects.toThrow();
  });
  it.each([
    ['wrong taxonomy', (value: FindingsResponseV4) => { value.navigation.declaration!.bindings[0].taxonomy.sha256 = 'f'.repeat(64); }],
    ['wrong source presentation', (value: FindingsResponseV4) => { value.navigation.declaration!.source_presentation.revision = 'other'; }],
    ['binding order', (value: FindingsResponseV4) => { value.navigation.declaration!.bindings.reverse(); }],
    ['missing task', (value: FindingsResponseV4) => { value.navigation.declaration!.bindings.pop(); }],
    ['unknown member', (value: FindingsResponseV4) => { value.navigation.declaration!.groups[0].member_ids.push('unknown'); }],
    ['duplicate member', (value: FindingsResponseV4) => { value.navigation.declaration!.groups[0].member_ids.push('synthetic-topic'); }],
    ['disabled category', (value: FindingsResponseV4) => { value.navigation.declaration!.bindings[1].enabled_ids.push('z-disabled'); }],
    ['unsafe label', (value: FindingsResponseV4) => { value.navigation.declaration!.groups[0].display_label = 'Unsafe\u202e'; }],
    ['surrogate', (value: FindingsResponseV4) => { value.navigation.declaration!.groups[0].display_label = '\ud800'; }],
    ['membership cap', (value: FindingsResponseV4) => { value.navigation.declaration!.groups = Array.from({ length: 17 }, (_, i) => ({ ...value.navigation.declaration!.groups[0], group_id: `g${i}` })); }],
  ])('rejects a correctly hashed but invalid %s declaration', async (_, mutate) => {
    const value = await groupedFindingsResponse(request); mutate(value); await rehash(value);
    await expect(parseGroupedFindings(value, request)).rejects.toThrow();
  });
  it('allows 256 Unicode codepoints but refuses a longer group label', async () => {
    const value = await groupedFindingsResponse(request);
    value.navigation.declaration!.groups[0].display_label = '😀'.repeat(256); await rehash(value);
    await expect(parseGroupedFindings(value, request)).resolves.toBeTruthy();
    value.navigation.declaration!.groups[0].display_label += 'a'; await rehash(value);
    await expect(parseGroupedFindings(value, request)).rejects.toThrow();
  });
  it('accepts a hashed 256-codepoint astral category label in both windows and rejects 257', async () => {
    for (const length of [256, 257]) {
      const value = await groupedFindingsResponse(request);
      const label = '😀'.repeat(length);
      for (const task of [...value.tasks, ...value.comparison.tasks]) {
        if (task.task !== 'message_topics') continue;
        const row = task.rows.find((item) => item.category_id === 'synthetic-topic')!;
        row.display_label = label;
        const body = { presentation: value.presentation, task_contract: task.task_contract, taxonomy: task.taxonomy,
          category_id: row.category_id, display_label: label, label_status: row.label_status };
        row.label_identity_sha256 = `sha256:${Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
          new TextEncoder().encode(navigationCanonicalJson(body)))), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
      }
      if (length === 256) {
        const result = await parseGroupedFindings(value, request);
        expect(result.tasks[0].rows[0].display_label).toHaveLength(512);
        expect(result.comparison.tasks[0].rows[0].display_label).toBe(label);
      } else await expect(parseGroupedFindings(value, request)).rejects.toThrow();
    }
  });
  it('retains a not-declared flat fallback without inventing group results', async () => {
    const value = await groupedFindingsResponse(request);
    value.navigation = { availability: 'not_declared', policy: null, declaration: null, current: [], previous: null };
    expect((await parseGroupedFindings(value, request)).tasks).toEqual(value.tasks);
    await expect(parseGroupedFindings(value, { ...request, expected_navigation_sha256: `sha256:${'1'.repeat(64)}` })).rejects.toThrow();
    value.navigation.previous = [];
    await expect(parseGroupedFindings(value, request)).rejects.toThrow();
  });
  it('requires a pin for either page and rejects a different retained policy', async () => {
    const value = await groupedFindingsResponse(request);
    const pin = `sha256:${value.navigation.policy!.sha256}`;
    const page = { ...request, evidence_offset: 1, evidence_limit: 1, expected_navigation_sha256: pin };
    await expect(parseGroupedFindings(await groupedFindingsResponse(page), page)).resolves.toBeTruthy();
    await expect(parseGroupedFindings(await groupedFindingsResponse(page), { ...page, expected_navigation_sha256: null })).rejects.toThrow();
    await expect(parseGroupedFindings(value, { ...request, expected_navigation_sha256: `sha256:${'f'.repeat(64)}` })).rejects.toThrow();
    await expect(parseGroupedFindings(value, { ...request, conversation_offset: 1 })).rejects.toThrow();
  });
  it('verifies grouped responses through the same bounded authenticated client', async () => {
    const value = await groupedFindingsResponse(request);
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(value)));
    const pinned = { ...request, expected_navigation_sha256: `sha256:${value.navigation.policy!.sha256}` };
    await expect(createFindingsClient({ fetcher }).read(pinned)).resolves.toMatchObject({ schema_version: 'analytics-findings.v4' });
    const url = new URL(buildFindingsUrl(pinned), 'https://example.invalid');
    expect(url.searchParams.get('view')).toBe('grouped');
    expect(url.searchParams.get('expected_navigation_sha256')).toBe(pinned.expected_navigation_sha256);
    expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
  });
});

describe('navigation policy lifecycle', () => {
  async function setup() {
    const read = vi.fn<FindingsClient['read']>(async (selected) => parseGroupedFindings(await groupedFindingsResponse(selected), selected));
    store = createFindingsStore({ read, evidence: vi.fn(async (reference) => findingsSource(reference)) }, { view: 'grouped' });
    store.actions.setContext(baseline, context(), 'all');
    await vi.waitFor(() => expect(store!.getState().status).toBe('ready'));
    return read;
  }
  it('retains the exact pin across filters/pages and clears it on explicit reload', async () => {
    const read = await setup(); const pin = `sha256:${(store!.getState().result as FindingsResponseV4).navigation.policy!.sha256}`;
    for (const [direction, conversation, category] of [
      ['inbound', null, null], ['inbound', `c1:${'2'.repeat(64)}`, null],
      ['all', null, { task: 'message_topics', category_id: 'synthetic-topic' }],
    ] as const) {
      store!.actions.setContext(baseline, context(), direction, conversation, category);
      await vi.waitFor(() => expect(store!.getState().status).toBe('ready'));
      expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ expected_navigation_sha256: pin, evidence_offset: 0, conversation_offset: 0 }), expect.any(AbortSignal));
    }
    await store!.actions.read(0);
    expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ expected_navigation_sha256: pin }), expect.any(AbortSignal));
    await store!.actions.reload();
    expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ expected_navigation_sha256: null }), expect.any(AbortSignal));
  });
  it('clears the pin for a different baseline or session', async () => {
    const read = await setup();
    store!.actions.setContext({ ...baseline, projection_generation: 3 }, context(), 'all');
    await vi.waitFor(() => expect(store!.getState().status).toBe('ready'));
    expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ expected_navigation_sha256: null }), expect.any(AbortSignal));
    const next = context(); next.session = { ...next.session!, connection_id: 'changed-session' };
    store!.actions.setContext({ ...baseline, projection_generation: 3 }, next, 'all');
    await vi.waitFor(() => expect(store!.getState().status).toBe('ready'));
    expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ expected_navigation_sha256: null }), expect.any(AbortSignal));
  });
});
