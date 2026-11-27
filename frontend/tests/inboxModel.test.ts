import assert from 'node:assert/strict';
import { describe, it } from 'vitest';

import {
  filterConversations,
  formatMessageDateHeading,
  messageCalendarKey,
} from '../src/components/inbox/inboxModel';
import type { ConversationRecord } from '../src/protocol';

function makeConversation(
  overrides: Partial<ConversationRecord> & { conversation_id: string },
): ConversationRecord {
  return {
    conversation_id: overrides.conversation_id,
    display_name: overrides.display_name ?? null,
    platform_user_id: overrides.platform_user_id ?? null,
    unread_count: overrides.unread_count ?? 0,
    last_message_at: overrides.last_message_at ?? '2026-11-20T10:00:00.000Z',
    latest_message: overrides.latest_message ?? null,
    coverage: overrides.coverage ?? {
      status: 'complete',
      boundary: 'history_start',
      earliest_available_at: '2026-11-10T10:00:00.000Z',
      latest_acquired_at: '2026-11-20T10:00:00.000Z',
      data_as_of: '2026-11-20T10:00:00.000Z',
      reason_code: null,
    },
  };
}

describe('filterConversations', () => {
  const sample: ConversationRecord[] = [
    makeConversation({
      conversation_id: 'conv-1',
      display_name: 'Marcus Vance',
      platform_user_id: '1001',
      unread_count: 2,
      latest_message: {
        message_id: 'm1',
        text: '<p>Loved the backstage studio photos!</p>',
        sent_at: '2026-11-20T10:00:00.000Z',
        direction: 'inbound',
        sentiment: 'positive',
      },
    }),
    makeConversation({
      conversation_id: 'conv-2',
      display_name: 'Elena Rostova',
      platform_user_id: '1002',
      unread_count: 0,
      latest_message: {
        message_id: 'm2',
        text: 'Sending the custom voice clip tonight.',
        sent_at: '2026-11-19T18:30:00.000Z',
        direction: 'outbound',
        sentiment: 'neutral',
      },
    }),
    makeConversation({
      conversation_id: 'conv-3',
      display_name: null,
      platform_user_id: '8842',
      unread_count: 1,
      latest_message: null,
    }),
  ];

  it('returns all conversations when query is blank and filter is all', () => {
    const result = filterConversations(sample, { query: '   ', filter: 'all' });
    assert.deepEqual(
      result.map((c) => c.conversation_id),
      ['conv-1', 'conv-2', 'conv-3'],
    );
  });

  it('filters by unread count when filter is unread', () => {
    const result = filterConversations(sample, { query: '', filter: 'unread' });
    assert.deepEqual(
      result.map((c) => c.conversation_id),
      ['conv-1', 'conv-3'],
    );
  });

  it('matches display name, platform ID, and stripped message preview case-insensitively', () => {
    assert.deepEqual(
      filterConversations(sample, { query: 'MARCUS', filter: 'all' }).map((c) => c.conversation_id),
      ['conv-1'],
    );
    assert.deepEqual(
      filterConversations(sample, { query: '8842', filter: 'all' }).map((c) => c.conversation_id),
      ['conv-3'],
    );
    assert.deepEqual(
      filterConversations(sample, { query: 'backstage studio', filter: 'all' }).map(
        (c) => c.conversation_id,
      ),
      ['conv-1'],
    );
  });

  it('combines unread filter and text query', () => {
    assert.deepEqual(
      filterConversations(sample, { query: 'voice clip', filter: 'unread' }),
      [],
    );
    assert.deepEqual(
      filterConversations(sample, { query: 'studio', filter: 'unread' }).map(
        (c) => c.conversation_id,
      ),
      ['conv-1'],
    );
  });
});

describe('messageCalendarKey & formatMessageDateHeading', () => {
  const now = new Date('2026-11-20T15:00:00.000Z');

  it('computes YYYY-MM-DD keys in the requested timezone and returns null on invalid input', () => {
    assert.equal(messageCalendarKey('2026-11-20T01:30:00.000Z', 'UTC'), '2026-11-20');
    assert.equal(messageCalendarKey('2026-11-20T01:30:00.000Z', 'America/Los_Angeles'), '2026-11-19');
    assert.equal(messageCalendarKey(null, 'UTC'), null);
    assert.equal(messageCalendarKey('not-a-date', 'UTC'), null);
  });

  it('formats Today, Yesterday, same-year dates, and cross-year dates accurately', () => {
    assert.equal(formatMessageDateHeading('2026-11-20T08:15:00.000Z', now, 'UTC'), 'Today');
    assert.equal(formatMessageDateHeading('2026-11-19T23:59:00.000Z', now, 'UTC'), 'Yesterday');
    assert.equal(formatMessageDateHeading('2026-11-14T12:00:00.000Z', now, 'UTC'), '14 Nov');
    assert.equal(formatMessageDateHeading('2025-11-14T12:00:00.000Z', now, 'UTC'), '14 Nov 2025');
    assert.equal(formatMessageDateHeading(null, now, 'UTC'), '');
    assert.equal(formatMessageDateHeading('invalid', now, 'UTC'), '');
  });
});
