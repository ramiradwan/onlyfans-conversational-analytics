import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import type { StateDeltaPayload, StateSnapshotPayload } from '../src/protocol';
import { createBridgeTransportStore } from '../src/store/transportStore';

const FIXTURES = resolve(process.cwd(), '../shared/fixtures/protocol/v1');

function payload<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(FIXTURES, `${name}.json`), 'utf8')).payload;
}

describe('durable Bridge read-model application', () => {
  it('fully replaces from snapshot and applies only the next revision atomically', () => {
    const store = createBridgeTransportStore();
    store.bindAccount('dev-creator-account');
    const snapshot = payload<StateSnapshotPayload>('state.snapshot');
    expect(store.applySnapshot(snapshot)).toBe(true);

    snapshot.conversations[0].messages[0].text = 'mutated outside the store';
    expect(store.getState().conversations[0].messages[0].text).toBe('Hello');

    const delta = payload<StateDeltaPayload>('state.delta');
    expect(store.applyDelta(delta)).toBe('applied');
    expect(store.getState().viewRevision).toBe(43);
    expect(store.applyDelta(delta)).toBe('duplicate');
    expect(store.getState().viewRevision).toBe(43);
  });

  it('rejects an invalid atomic change set without changing data or revision', () => {
    const store = createBridgeTransportStore();
    store.bindAccount('dev-creator-account');
    store.applySnapshot(payload<StateSnapshotPayload>('state.snapshot'));
    const invalid = payload<StateDeltaPayload>('state.delta');
    invalid.changes.push({ ...invalid.changes[0] });

    expect(store.applyDelta(invalid)).toBe('invalid');
    expect(store.getState().viewRevision).toBe(42);
    expect(store.getState().analytics.total_messages).toBe(1);
  });
});
