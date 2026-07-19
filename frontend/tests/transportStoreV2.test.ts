import { describe, expect, it } from 'vitest';

import type {
  AnalyticsMetric,
  MessagePageResponse,
  MessageView,
  StateSnapshotPayload,
} from '../src/protocol';
import {
  DEFAULT_MESSAGE_PAGE_SIZE,
  MAX_ACTIVE_MESSAGE_PAGES,
  MAX_CACHED_MESSAGES_PER_CONVERSATION,
  MAX_INACTIVE_MESSAGE_PAGES,
  createBridgeTransportStore,
} from '../src/store/transportStore';
import { formatAdditiveMetric, isFullyCurrent } from '../src/utils/dataReadiness';

function metric(value: number): AnalyticsMetric {
  return {
    value,
    basis: 'synced_subset',
    observed_range: {
      start: '2026-07-18T12:00:00Z',
      end: '2026-07-19T12:00:00Z',
    },
    complete_range: null,
    sample_size: 1,
    as_of: '2026-07-19T12:00:00Z',
    projection_revision: 12,
  };
}

function snapshot(): StateSnapshotPayload {
  return {
    creator_account_id: 'creator-1',
    view_revision: 12,
    generated_at: '2026-07-19T12:00:00Z',
    conversations: [
      {
        conversation_id: 'conversation-1',
        platform_user_id: 'fan-1',
        display_name: 'Fan One',
        unread_count: 0,
        last_message_at: null,
        latest_message: null,
        coverage: {
          status: 'partial',
          boundary: null,
          earliest_available_at: null,
          latest_acquired_at: null,
          data_as_of: '2026-07-19T12:00:00Z',
          reason_code: null,
        },
      },
    ],
    analytics: {
      total_conversations: metric(1),
      total_messages: metric(0),
      inbound_messages: metric(0),
      outbound_messages: metric(0),
    },
    coverage: {
      status: 'partial',
      phase: 'backfilling',
      generation_id: '10000000-0000-4000-8000-000000000001',
      as_of: '2026-07-19T12:00:00Z',
      discovered_conversations: 4,
      complete_conversations: 1,
      complete_as_of: null,
      reason: null,
    },
    projection: {
      status: 'current',
      canonical_revision: 12,
      projected_revision: 12,
      projected_at: '2026-07-19T12:00:00Z',
      reason: null,
    },
    live_freshness: {
      status: 'current',
      last_observed_at: '2026-07-19T11:59:59Z',
      last_committed_at: '2026-07-19T12:00:00Z',
      expires_at: '2026-07-19T12:02:00Z',
      pending_count: 0,
      reason: null,
    },
  };
}

function page(overrides: Partial<MessagePageResponse> = {}): MessagePageResponse {
  return {
    creator_account_id: 'creator-1',
    conversation_id: 'conversation-1',
    projection_generation: 'projection-a',
    read_revision: 12,
    generated_at: '2026-07-19T12:00:00Z',
    items: [],
    older_cursor: null,
    has_older_stored_items: false,
    conversation_coverage: snapshot().conversations[0].coverage,
    projection: snapshot().projection,
    ...overrides,
  };
}

function messages(start: number, count = DEFAULT_MESSAGE_PAGE_SIZE): MessageView[] {
  return Array.from({ length: count }, (_, offset) => {
    const index = start + offset;
    return {
      message_id: `message-${index.toString().padStart(4, '0')}`,
      text: String(index),
      sent_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
      direction: index % 2 ? 'outbound' : 'inbound',
      sentiment: 'unknown',
    };
  });
}

describe('protocol-v2 transport store', () => {
  it('keeps coverage, projection, freshness, and configuration alignment independent', () => {
    const store = createBridgeTransportStore();
    expect(store.getState().analytics).toBeNull();
    store.bindAccount('creator-1');
    expect(store.applySnapshot(snapshot())).toBe(true);

    const state = store.getState();
    const readiness = {
      coverage: state.coverage,
      projection: state.projection,
      liveFreshness: state.liveFreshness,
      configurationAligned: false,
    };
    expect(state.snapshotProgress.percentage).toBe(25);
    expect(state.coverage.status).toBe('partial');
    expect(state.projection.status).toBe('current');
    expect(state.liveFreshness.status).toBe('current');
    expect(isFullyCurrent(readiness)).toBe(false);
    expect(formatAdditiveMetric(state.analytics?.total_conversations, readiness)).toBe('1+');
    expect(formatAdditiveMetric(state.analytics?.total_messages, readiness)).toBe(
      '0 in synced messages',
    );
  });

  it('keeps 20 active pages, trims inactive conversations to two pages, and rejects stale cursors', () => {
    const store = createBridgeTransportStore();
    store.bindAccount('creator-1');
    store.applySnapshot(snapshot());
    store.setActiveConversation('conversation-1');

    const newestPageStart = MAX_ACTIVE_MESSAGE_PAGES * DEFAULT_MESSAGE_PAGE_SIZE;
    store.beginMessagePage('conversation-1');
    expect(
      store.applyMessagePage(
        page({
          items: messages(newestPageStart),
          older_cursor: `before-${newestPageStart}`,
          has_older_stored_items: true,
        }),
      ),
    ).toBe('applied');

    for (let pageIndex = MAX_ACTIVE_MESSAGE_PAGES - 1; pageIndex >= 0; pageIndex -= 1) {
      const start = pageIndex * DEFAULT_MESSAGE_PAGE_SIZE;
      expect(
        store.applyMessagePage(
          page({
            items: messages(start),
            older_cursor: pageIndex === 0 ? null : `before-${start}`,
            has_older_stored_items: pageIndex !== 0,
          }),
          'prepend',
        ),
      ).toBe('applied');
    }

    expect(MAX_CACHED_MESSAGES_PER_CONVERSATION).toBe(
      DEFAULT_MESSAGE_PAGE_SIZE * MAX_ACTIVE_MESSAGE_PAGES,
    );
    expect(store.getState().messagePages['conversation-1'].items).toHaveLength(
      DEFAULT_MESSAGE_PAGE_SIZE * MAX_ACTIVE_MESSAGE_PAGES,
    );

    store.setActiveConversation(null);
    expect(store.getState().messagePages['conversation-1'].items).toHaveLength(
      DEFAULT_MESSAGE_PAGE_SIZE * MAX_INACTIVE_MESSAGE_PAGES,
    );

    expect(
      store.applyMessagePage(
        page({ projection_generation: 'projection-b', read_revision: 13 }),
        'prepend',
      ),
    ).toBe('stale');
    expect(store.getState().messagePages['conversation-1'].projectionGeneration).toBe(
      'projection-a',
    );
  });

  it('rejects a delayed initial page after a newer projection activates', () => {
    const store = createBridgeTransportStore();
    store.bindAccount('creator-1');
    store.applySnapshot(snapshot());
    const requestEpoch = store.beginMessagePage('conversation-1', true);

    const nextSnapshot = snapshot();
    nextSnapshot.view_revision = 13;
    nextSnapshot.generated_at = '2026-07-19T12:01:00Z';
    nextSnapshot.projection = {
      ...nextSnapshot.projection,
      canonical_revision: 13,
      projected_revision: 13,
      projected_at: '2026-07-19T12:01:00Z',
    };
    expect(store.applySnapshot(nextSnapshot)).toBe(true);

    expect(
      store.applyMessagePage(
        page({ items: messages(0, 1) }),
        'replace',
        requestEpoch,
      ),
    ).toBe('stale');
    expect(store.getState().messagePages['conversation-1']).toBeUndefined();
  });
});
