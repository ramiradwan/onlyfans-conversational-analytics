import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import type {
  MessagePageResponse,
  StateDeltaPayload,
  StateSnapshotPayload,
} from '../src/protocol';
import { createBridgeTransportStore } from '../src/store/transportStore';

const FIXTURES = resolve(process.cwd(), '../shared/fixtures/protocol/v2');

function payload<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(FIXTURES, `${name}.json`), 'utf8')).payload;
}

describe('durable protocol-v2 Bridge read model', () => {
  it('clones a bounded summary snapshot and applies only the next metric-envelope revision', () => {
    const store = createBridgeTransportStore();
    store.bindAccount('dev-creator-account');
    const snapshot = payload<StateSnapshotPayload>('state.snapshot');
    expect(store.applySnapshot(snapshot)).toBe(true);

    snapshot.conversations[0].latest_message!.text = 'mutated outside the store';
    snapshot.conversations[0].coverage.reason_code = 'mutated';
    snapshot.analytics.total_messages.observed_range.start = null;
    expect(store.getState().conversations[0].latest_message?.text).toBe('Hello');
    expect(store.getState().conversations[0].coverage.reason_code).toBe(
      'history_boundary_not_observed',
    );
    expect(store.getState().analytics?.total_messages.observed_range.start).toBe(
      '2026-07-19T09:59:00Z',
    );
    expect('messages' in store.getState().conversations[0]).toBe(false);
    expect(store.getState().messagePages).toEqual({});

    const delta = payload<StateDeltaPayload>('state.delta');
    expect(store.applyDelta(delta)).toBe('applied');
    expect(store.getState().viewRevision).toBe(43);
    expect(store.getState().analytics?.total_messages).toMatchObject({
      value: 2,
      basis: 'synced_subset',
      sample_size: 2,
      projection_revision: 12,
    });
    expect(store.applyDelta(delta)).toBe('duplicate');
    expect(store.getState().viewRevision).toBe(43);
  });

  it('rejects a duplicate atomic change target without changing any published state', () => {
    const store = createBridgeTransportStore();
    store.bindAccount('dev-creator-account');
    store.applySnapshot(payload<StateSnapshotPayload>('state.snapshot'));
    const before = structuredClone(store.getState());
    const invalid = payload<StateDeltaPayload>('state.delta');
    invalid.changes.push({ ...invalid.changes[0] });

    expect(store.applyDelta(invalid)).toBe('invalid');
    expect(store.getState()).toEqual(before);
    expect(store.getState().analytics?.total_messages.value).toBe(1);
  });

  it('keeps full message bodies in bounded REST-page state, never on summaries', () => {
    const store = createBridgeTransportStore();
    store.bindAccount('dev-creator-account');
    const snapshot = payload<StateSnapshotPayload>('state.snapshot');
    store.applySnapshot(snapshot);
    store.setActiveConversation('chat-1');
    store.beginMessagePage('chat-1');

    const page: MessagePageResponse = {
      creator_account_id: 'dev-creator-account',
      conversation_id: 'chat-1',
      projection_generation: 'projection-generation-11',
      read_revision: 42,
      generated_at: snapshot.generated_at,
      items: [
        {
          message_id: 'message-1',
          text: 'Full body from REST',
          sent_at: '2026-07-19T09:59:00Z',
          direction: 'inbound',
          sentiment: 'positive',
        },
      ],
      older_cursor: 'authenticated-opaque-cursor',
      has_older_stored_items: true,
      conversation_coverage: snapshot.conversations[0].coverage,
      projection: snapshot.projection,
    };

    expect(store.applyMessagePage(page)).toBe('applied');
    expect(store.getState().messagePages['chat-1'].items[0].text).toBe(
      'Full body from REST',
    );
    expect(store.getState().conversations[0].latest_message?.text).toBe('Hello');
    expect('messages' in store.getState().conversations[0]).toBe(false);
  });

  it('clears summaries, pages, analytics, and all readiness dimensions on account switch', () => {
    const store = createBridgeTransportStore();
    store.bindAccount('dev-creator-account');
    const snapshot = payload<StateSnapshotPayload>('state.snapshot');
    store.applySnapshot(snapshot);
    store.beginMessagePage('chat-1');

    store.bindAccount('another-account');

    const state = store.getState();
    expect(state.creatorAccountId).toBe('another-account');
    expect(state.conversations).toEqual([]);
    expect(state.messagePages).toEqual({});
    expect(state.analytics).toBeNull();
    expect(state.coverage.status).toBe('unknown');
    expect(state.projection.status).toBe('pending');
    expect(state.liveFreshness.status).toBe('unknown');
    expect(state.viewRevision).toBeNull();
  });
});
