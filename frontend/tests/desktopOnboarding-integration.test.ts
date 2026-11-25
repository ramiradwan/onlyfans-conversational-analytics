
import { afterEach, expect, it, vi } from 'vitest';
import { desktopOnboardingApi as api } from '../src/services/desktopOnboarding';
import { SCOPED_SETUP_EVENT } from '../onboarding/events';

const generation = '11111111-1111-4111-8111-111111111111';
const taskID = '22222222-2222-4222-8222-222222222222';
const journeyID = '33333333-3333-4333-8333-333333333333';
const accessOperationId = '44444444-4444-4444-8444-444444444444';
const notice = { task_id: taskID, operation_id: accessOperationId,
  admission_generation: '55555555-5555-4555-8555-555555555555', reason: 'profile_mismatch' };
const session = { role: 'desktop', stage: 'runtime', generation, csrf_token: 'A'.repeat(43) };
const snapshot = { profile: 'desktop-onboarding.v2', kind: 'snapshot', journey_id: journeyID, generation,
  revision: 1, scope_digest: 'a'.repeat(64), expires_at: 2000000000,
  facts: Object.fromEntries(['browser_access', 'installation', 'account', 'configuration', 'enrollment', 'pairing', 'activation', 'analysis'].map(name => [name, ['activation', 'analysis'].includes(name) ? 'missing' : 'verified'])),
  next_task: 'complete', local_ready: true, full_ready: false, allowed_actions: ['activation.review'], reason: 'none',
  pending_operations: [{ operation_id: accessOperationId, action: 'browser.access', state: 'waiting', reason: 'none' }],
  analysis_readiness: { availability: 'unauthorized', creator_account_id: null, value: null } };

afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

it.each([['completed', 'completed'], ['waiting', 'accepted'], ['outcome_unknown', 'unconfirmed']])('returns %s for its own activation interaction', async (receiptState, expectedState) => {
  sessionStorage.setItem('desktop-presentation', generation);
  const receipts: string[] = [];
  let operationId = '';
  vi.stubGlobal('fetch', async (path: string, init?: RequestInit) => {
    if (path.endsWith('/session')) return Response.json(session);
    if (path.endsWith('/events')) {
      return new Response(new ReadableStream({ start(stream) {
        const text = `event: ${SCOPED_SETUP_EVENT}\ndata: ${JSON.stringify({ snapshot, browser_notices: [notice] })}\n\n`;
        stream.enqueue(new TextEncoder().encode(text));
      } }), { headers: { 'Content-Type': 'text/event-stream' } });
    }
    if (path.endsWith('/timing')) return new Response(null, { status: 204 });
    if (path.endsWith('/commands')) {
      operationId = JSON.parse(String(init?.body)).operation_id;
      return Response.json({ operation_id: operationId, action: 'activation.review', state: 'accepted', reason: 'none' }, { status: 202 });
    }
    if (path.endsWith('/operations/' + operationId) && operationId) {
      receipts.push(operationId);
      return Response.json({ operation_id: operationId, action: 'activation.review', state: receiptState, reason: 'none' });
    }
    throw Error('Unexpected fixture request');
  });
  const unsubscribe = api.subscribe(() => {});
  try {
    await vi.waitFor(() => expect(api.getState().connected).toBe(true));
    // The scoped frame retains the unrelated, still-current browser notice.
    expect(api.getState().snapshot?.allowed_actions).toContain('activation.review');
    expect(api.getState().browserNotices).toEqual([notice]);
    await expect(api.begin('activation.review')).resolves.toMatchObject({ state: expectedState });
    expect(receipts).toEqual([operationId]);
  } finally { unsubscribe(); }
});
