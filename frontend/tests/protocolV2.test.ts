import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { isBrainToBridgeMessage, parseBrainToBridgeMessage } from '../src/protocol';

const FIXTURES = resolve(process.cwd(), '../shared/fixtures/protocol/v2');

function fixture(name: string): Record<string, any> {
  return JSON.parse(readFileSync(resolve(FIXTURES, `${name}.json`), 'utf8')) as Record<
    string,
    any
  >;
}

describe('Bridge protocol v2', () => {
  it('accepts bounded summaries, metric envelopes, and independent readiness dimensions', () => {
    const parsed = parseBrainToBridgeMessage(fixture('state.snapshot'));
    expect(parsed.protocol_version).toBe('2');
    expect(parsed.type).toBe('state.snapshot');
    if (parsed.type !== 'state.snapshot') return;

    const conversation = parsed.payload.conversations[0];
    expect(conversation.latest_message?.text).toBe('Hello');
    expect(conversation).not.toHaveProperty('messages');

    const metric = parsed.payload.analytics.total_messages;
    expect(metric).toMatchObject({
      value: 1,
      basis: 'synced_subset',
      sample_size: 1,
      projection_revision: 11,
    });
    expect(metric.observed_range).toEqual({
      start: '2026-07-19T09:59:00Z',
      end: '2026-07-19T10:01:00Z',
    });
    expect(metric.complete_range).toBeNull();

    expect(parsed.payload.coverage).toMatchObject({
      status: 'partial',
      as_of: '2026-07-19T10:00:00Z',
      complete_as_of: null,
      reason: 'conversation_evidence_missing',
    });
    expect(parsed.payload.projection).toMatchObject({
      status: 'current',
      canonical_revision: 11,
      projected_revision: 11,
      reason: null,
    });
    expect(parsed.payload.live_freshness).toMatchObject({
      status: 'current',
      pending_count: 0,
      reason: null,
    });
  });

  it('rejects unbounded message arrays and superseded readiness fields', () => {
    const unbounded = fixture('state.snapshot');
    unbounded.payload.conversations[0].messages = [];
    expect(isBrainToBridgeMessage(unbounded)).toBe(false);

    const legacyReadiness = fixture('state.snapshot');
    legacyReadiness.payload.coverage.data_as_of = legacyReadiness.payload.coverage.as_of;
    expect(isBrainToBridgeMessage(legacyReadiness)).toBe(false);
  });

  it('accepts dimension-only next-revision deltas with the locked coverage vocabulary', () => {
    const snapshot = fixture('state.snapshot');
    const delta = {
      type: 'state.delta',
      protocol_version: '2',
      message_id: '20000000-0000-4000-8000-000000000002',
      payload: {
        creator_account_id: snapshot.payload.creator_account_id,
        view_revision: snapshot.payload.view_revision + 1,
        committed_at: '2026-07-19T10:03:00Z',
        changes: [
          {
            type: 'coverage.replace',
            coverage: {
              ...snapshot.payload.coverage,
              status: 'complete',
              phase: 'complete',
              complete_conversations: snapshot.payload.coverage.discovered_conversations,
              complete_as_of: '2026-07-19T10:03:00Z',
              reason: null,
            },
          },
        ],
      },
    };
    expect(isBrainToBridgeMessage(delta)).toBe(true);
  });
});
