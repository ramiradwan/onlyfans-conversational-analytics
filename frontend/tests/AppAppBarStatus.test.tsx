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

describe('AppBar locked readiness priority', () => {
  it('shows Action needed before every other condition for protocol or Agent issues', () => {
    const presentation = getStatusPresentation(
      state({
        protocolError: {
          code: 'identity_conflict',
          related_message_id: null,
          retryable: false,
          fatal: true,
          detail: 'Bound account does not match the signer identity.',
        },
        viewRevision: null,
        projection: { ...state().projection, status: 'unavailable' },
        liveFreshness: { ...state().liveFreshness, status: 'delayed' },
      }),
    );

    expect(presentation.label).toBe('Action needed');
    expect(presentation.detail).toContain('Bound account does not match');
  });

  it('requires both config and history-settings revisions to be applied exactly', () => {
    const configMismatch = state({
      agent: {
        ...state().agent!,
        applied_config_revision: 'config-7',
      },
    });
    const settingsMismatch = state({
      agent: {
        ...state().agent!,
        applied_history_settings_revision: 11,
      },
    });
    const missingAppliedRevision = state({
      agent: {
        ...state().agent!,
        applied_history_settings_revision: null,
      },
    });

    expect(getStatusPresentation(configMismatch).label).toBe('Action needed');
    expect(getStatusPresentation(settingsMismatch).label).toBe('Action needed');
    expect(getStatusPresentation(missingAppliedRevision).label).toBe('Action needed');
    expect(getStatusPresentation(state()).label).toBe('Up to date');
  });

  it('shows Data unavailable when no valid projection exists and no action issue outranks it', () => {
    expect(
      getStatusPresentation(
        state({
          viewRevision: null,
          projection: {
            ...state().projection,
            status: 'unavailable',
            reason: 'projection_generation_failed',
          },
        }),
      ).label,
    ).toBe('Data unavailable');
  });

  it('shows Updates delayed ahead of paused coverage or a lagging projection', () => {
    const partial = partialState('paused');
    expect(
      getStatusPresentation({
        ...partial,
        liveFreshness: {
          ...partial.liveFreshness,
          status: 'delayed',
          reason: 'agent_heartbeat_late',
        },
        projection: {
          ...partial.projection,
          canonical_revision: 9,
          projected_revision: 8,
        },
      }).label,
    ).toBe('Updates delayed');
  });

  it('distinguishes paused and running partial acquisition', () => {
    expect(getStatusPresentation(partialState('paused')).label).toBe('History paused');
    const running = getStatusPresentation(partialState('backfilling'));
    expect(running.label).toBe('Syncing history');
    expect(running.detail).toContain('Historical coverage 50%');
  });

  it('shows Updating insights after complete acquisition until projection catches up', () => {
    const behind = state({
      projection: {
        ...state().projection,
        canonical_revision: 9,
        projected_revision: 8,
      },
    });

    expect(getStatusPresentation(behind).label).toBe('Updating insights');
  });

  it('uses Up to date only when coverage, projection, freshness, and configuration align', () => {
    expect(getStatusPresentation(state()).label).toBe('Up to date');
    expect(
      getStatusPresentation(
        state({ liveFreshness: { ...state().liveFreshness, status: 'unknown' } }),
      ).label,
    ).toBe('Updates delayed');
    expect(
      getStatusPresentation(
        state({ projection: { ...state().projection, status: 'degraded' } }),
      ).label,
    ).toBe('Updating insights');
    expect(getStatusPresentation(partialState('backfilling')).label).toBe(
      'Syncing history',
    );
  });
});
