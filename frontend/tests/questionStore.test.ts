import { webcrypto } from 'node:crypto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { QuestionResult } from '../src/analytics/questionContract';
import type { QuestionApi } from '../src/services/questionApi';
import { createQuestionStore, type QuestionStore } from '../src/store/questionStore';
import { answer, catalog, context, evidence, NOW, plan, thread } from './questionFixture';

let controller: QuestionStore;
beforeEach(() => { vi.stubGlobal('crypto', webcrypto); vi.useFakeTimers(); vi.setSystemTime(NOW); });
afterEach(() => { controller?.actions.dispose(); vi.useRealTimers(); vi.unstubAllGlobals(); });
async function setup(overrides: Partial<QuestionApi> = {}) {
  const api: QuestionApi = { catalog: vi.fn(async () => catalog), answer: vi.fn(async (p) => answer(p)),
    evidence: vi.fn(async () => evidence()), clear: vi.fn(async () => undefined), ...overrides };
  const getPage = vi.fn(async () => thread());
  controller = createQuestionStore(api, { getPage });
  controller.actions.setContext(context());
  await Promise.resolve();
  return { api, getPage };
}
describe('Question lifecycle', () => {
  it('loads a page and resolves the exact source and conversation', async () => {
    const { getPage } = await setup();
    await controller.actions.run(plan);
    expect(controller.getState().status).toBe('ready');
    await controller.actions.openSource(answer().page.rows[0].evidence[0]);
    expect(controller.getState().source?.location.conversation_id).toBe('synthetic-chat');
    await controller.actions.openConversation();
    expect(getPage).toHaveBeenCalledOnce(); expect(controller.getState().thread?.items).toHaveLength(1);
  });
  it.each(['account', 'revision', 'disconnect', 'dispose'])('drops late results after %s changes', async (change) => {
    let finish!: (value: QuestionResult) => void;
    let started!: () => void;
    const began = new Promise<void>((resolve) => { started = resolve; });
    await setup({ answer: async () => { started(); return new Promise((resolve) => { finish = resolve; }); } });
    const pending = controller.actions.run(plan);
    await began;
    if (change === 'dispose') controller.actions.dispose();
    else {
      const next = context();
      if (change === 'account') next.creatorAccountId = 'synthetic-other';
      if (change === 'revision') next.projection.canonical_revision = 8;
      if (change === 'disconnect') next.connection = 'disconnected' as typeof next.connection;
      controller.actions.setContext(next);
    }
    finish(answer()); await pending;
    expect(controller.getState().result).toBeNull();
    expect(controller.getState().source).toBeNull();
  });
  it('does not show a first page for another account', async () => {
    await setup({ answer: async () => { const value = answer(); value.question.account_ref = `a1:${'9'.repeat(64)}`; return value; } });
    await controller.actions.run(plan);
    expect(controller.getState().status).toBe('error'); expect(controller.getState().result).toBeNull();
  });
  it('expires displayed source text without another request', async () => {
    await setup(); await controller.actions.run(plan);
    await controller.actions.openSource(answer().page.rows[0].evidence[0]);
    await vi.advanceTimersByTimeAsync(15 * 60_000);
    expect(controller.getState().source).toBeNull(); expect(controller.getState().status).toBe('stale');
  });
  it('replaces pages and rejects a changed generation', async () => {
    await setup({ answer: async (p) => {
      const value = answer(p, p.cursor ? 2 : 1);
      value.next_cursor = p.cursor ? null : 'synthetic-next';
      value.page.has_more = !p.cursor; value.page.total_matching_conversations = 2;
      if (p.cursor) value.question.snapshot.projection_generation = 3;
      return value;
    } });
    await controller.actions.run(plan);
    expect(controller.getState().result?.page.rows).toHaveLength(1);
    await controller.actions.next();
    expect(controller.getState().result).toBeNull(); expect(controller.getState().status).toBe('error');
  });
  it('keeps disabled pricing out of requests', async () => {
    const { api } = await setup();
    await controller.actions.run({ ...plan, question: 'pricing_discussions.v1' });
    expect(api.answer).not.toHaveBeenCalled();
  });
  it('does not open a source that was not returned in the current page', async () => {
    const { api } = await setup(); await controller.actions.run(plan);
    await controller.actions.openSource(answer(plan, 2).page.rows[0].evidence[0]);
    expect(api.evidence).not.toHaveBeenCalled();
  });
  it('refuses conversation history from another account or revision', async () => {
    const { getPage } = await setup(); await controller.actions.run(plan);
    await controller.actions.openSource(answer().page.rows[0].evidence[0]);
    getPage.mockResolvedValue({ ...thread(), creator_account_id: 'synthetic-other' });
    await controller.actions.openConversation();
    expect(controller.getState().thread).toBeNull(); expect(controller.getState().source).toBeNull();
  });
});

it('pages backward without mixing rows and without extending result expiry', async () => {
  await setup({ answer: async (p) => {
    const value = answer(p, p.cursor ? 2 : 1);
    value.next_cursor = p.cursor ? null : 'synthetic-next';
    value.page.has_more = !p.cursor; value.page.total_matching_conversations = 2;
    return value;
  } });
  await controller.actions.run(plan);
  const first = controller.getState().result?.page.rows[0].conversation_ref;
  await vi.advanceTimersByTimeAsync(10 * 60_000);
  await controller.actions.next();
  expect(controller.getState().pageNumber).toBe(2);
  expect(controller.getState().result?.page.rows).toHaveLength(1);
  expect(controller.getState().result?.page.rows[0].conversation_ref).not.toBe(first);
  await controller.actions.previous();
  expect(controller.getState().pageNumber).toBe(1);
  expect(controller.getState().result?.page.rows[0].conversation_ref).toBe(first);
  await vi.advanceTimersByTimeAsync(5 * 60_000);
  expect(controller.getState().result).toBeNull();
  expect(controller.getState().status).toBe('stale');
});
