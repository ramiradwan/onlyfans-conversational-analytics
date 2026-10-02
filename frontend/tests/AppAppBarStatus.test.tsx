import { describe, expect, it } from 'vitest';

import { getStatusPresentation } from '../src/layouts/AppAppBar';
import type { BridgeTransportState } from '../src/store/transportStore';
import { createBridgeTransportStore } from '../src/store/transportStore';

const AS_OF = '2026-07-19T12:00:00Z';

function state(overrides: Partial<BridgeTransportState> = {}): BridgeTransportState {
  const initial = createBridgeTransportStore().getState();
  return {
    ...initial,
    connection: 'connected',
    creatorAccountId: 'creator-1',
    readModelState: 'realtime',
    viewRevision: 8,
    agent: {
      creator_account_id: 'creator-1',
      status: 'connected',
      agent_installation_id: '20000000-0000-4000-8000-000000000001',
      connection_id: '10000000-0000-4000-8000-000000000001',
      required_config_revision: 'config-8',
      applied_config_revision: 'config-8',
      required_history_settings_revision: 12,
      applied_history_settings_revision: 12,
      last_heartbeat_at: AS_OF,
      degraded_reason: null,
      browser: null,
    },
    system: {
      creator_account_id: 'creator-1',
      processing_mode: 'realtime',
      readiness: 'ready',
      updated_at: AS_OF,
      detail: null,
    },
    coverage: {
      status: 'complete',
      phase: 'complete',
      generation_id: '90000000-0000-4000-8000-000000000001',
      as_of: AS_OF,
      discovered_conversations: 4,
      complete_conversations: 4,
      complete_as_of: AS_OF,
      reason: null,
    },
    projection: {
      status: 'current',
      canonical_revision: 8,
      projected_revision: 8,
      projected_at: AS_OF,
      reason: null,
    },
    liveFreshness: {
      status: 'current',
      last_observed_at: AS_OF,
      last_committed_at: AS_OF,
      expires_at: '2026-07-19T12:02:00Z',
      pending_count: 0,
      reason: null,
    },
    snapshotProgress: {
      phase: 'complete',
      discoveredConversations: 4,
      completeConversations: 4,
      partialConversations: 0,
      percentage: 100,
    },
    ...overrides,
  };
}

function partialState(phase: 'backfilling' | 'paused'): BridgeTransportState {
  return state({
    coverage: {
      ...state().coverage,
      status: 'partial',
      phase,
      complete_conversations: 2,
      complete_as_of: null,
      reason: phase === 'paused' ? 'paused_by_creator' : 'conversation_evidence_missing',
    },
    snapshotProgress: {
      phase,
      discoveredConversations: 4,
      completeConversations: 2,
      partialConversations: 2,
      percentage: 50,
    },
  });
}

describe('AppBar freshness authority', () => {
  it('keeps fatal protocol errors actionable', () => {
    const result = getStatusPresentation(state({ protocolError: { code: 'identity_conflict', related_message_id: null, retryable: false, fatal: true, detail: 'internal detail' } }));
    expect(result.label).toBe('Action needed');
    expect(result.detail).not.toContain('internal detail');
  });
  it('does not manufacture freshness from metrics, history, configuration or liveness', () => {
    for (const input of [state(), partialState('paused'), partialState('backfilling'), state({ projection: { ...state().projection, status: 'unavailable' } }), state({ agent: { ...state().agent!, applied_config_revision: null } })]) {
      expect(getStatusPresentation(input).label).toBe('Messages not checked');
    }
  });
  it('follows catch-up state independently of the live arrival indicator', () => {
    const catchupFreshness = { status: 'current' as const, reason: null, uncertain_since: null, last_closed_at: AS_OF, observing_since: AS_OF };
    expect(getStatusPresentation(state({ catchupFreshness, liveFreshness: { ...state().liveFreshness, status: 'unknown' } })).label).toBe('Up to date');
    expect(getStatusPresentation(state({ catchupFreshness: { ...catchupFreshness, status: 'paused', reason: 'applying_settings' } })).label).toBe('Paused · applying settings');
    expect(getStatusPresentation(state({ catchupFreshness, viewRevision: null })).label).toBe('Messages not checked');
    expect(getStatusPresentation(state({ catchupFreshness, connection: 'disconnected' })).label).toBe('Paused · Brain offline');
  });
});
