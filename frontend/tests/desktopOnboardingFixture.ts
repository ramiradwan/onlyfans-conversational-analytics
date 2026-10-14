import { vi } from 'vitest';
import type { AnalysisReadiness, DesktopState } from '../onboarding/client.js';
import type { DesktopOnboardingApi } from '../src/services/desktopOnboarding';

export function desktopFixture(available = true) {
  let state: DesktopState = { connected: true, pending: null, error: null, snapshot: {
    journey_id: '11111111-1111-4111-8111-111111111111', generation: '22222222-2222-4222-8222-222222222222',
    revision: 0, facts: {}, local_ready: true, full_ready: false, next_task: 'activation',
    analysis_readiness: { availability: 'current', creator_account_id: 'creator-account', value: {
      schema: 'ofca-analysis-readiness/v1', commercial_authority: 'required', analysis_admission: 'blocked' } },
    pending_operations: [], allowed_actions: available ? ['activation.review'] : [],
  } };
  const listeners = new Set<() => void>();
  const api: DesktopOnboardingApi = {
    getState: () => state,
    subscribe(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    begin: vi.fn(async () => {}), reconcile: vi.fn(async () => {}), openBrowser: vi.fn(async () => {}),
  };
  const publish = (change: Partial<DesktopState> = {}) => {
    state = { ...state, ...change, snapshot: change.snapshot === undefined && state.snapshot
      ? { ...state.snapshot, revision: state.snapshot.revision + 1 } : change.snapshot ?? null };
    for (const listener of listeners) listener();
  };
  return { api, publish, readiness(value: AnalysisReadiness | null, creatorAccountId = 'creator-account') {
    publish({ snapshot: { ...state.snapshot!, analysis_readiness: value === null
      ? { availability: 'unknown', creator_account_id: creatorAccountId, value: null }
      : { availability: 'current', creator_account_id: creatorAccountId, value } } });
  } };
}
