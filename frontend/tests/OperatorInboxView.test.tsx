import { ThemeProvider } from '@mui/material/styles';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AnalyticsMetric,
  ConversationCoverage,
  ConversationSummary,
  MessagePageResponse,
  MessageView,
  StateSnapshotPayload,
} from '../src/protocol';
import type { MessageApi } from '../src/services/messageApi';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';
import OperatorInboxView from '../src/views/OperatorInboxView';

const ACCOUNT_ID = 'creator-account';
const AS_OF = '2026-07-18T13:00:00Z';

const PARTIAL_COVERAGE: ConversationCoverage = {
  status: 'partial',
  boundary: null,
  earliest_available_at: null,
  latest_acquired_at: AS_OF,
  data_as_of: AS_OF,
  reason_code: 'history_boundary_not_observed',
};

const COMPLETE_CONVERSATION_COVERAGE: ConversationCoverage = {
  status: 'complete',
  boundary: 'history_start',
  earliest_available_at: '2026-01-01T00:00:00Z',
  latest_acquired_at: AS_OF,
  data_as_of: AS_OF,
  reason_code: null,
};

function message(
  messageId: string,
  text: string,
  sentAt: string,
  direction: MessageView['direction'] = 'inbound',
  sentiment: MessageView['sentiment'] = 'neutral',
): MessageView {
  return { message_id: messageId, text, sent_at: sentAt, direction, sentiment };
}

function conversation(
  conversationId: string,
  displayName: string,
  lastMessageAt: string,
  preview: MessageView | null,
  unreadCount = 0,
  coverage: ConversationCoverage = PARTIAL_COVERAGE,
): ConversationSummary {
  return {
    conversation_id: conversationId,
    platform_user_id: `fan-${conversationId}`,
    display_name: displayName,
    unread_count: unreadCount,
    last_message_at: lastMessageAt,
    latest_message: preview,
    coverage,
  };
}

function metric(value: number): AnalyticsMetric {
  return {
    value,
    basis: 'synced_subset',
    observed_range: { start: null, end: AS_OF },
    complete_range: null,
    sample_size: value,
    as_of: AS_OF,
    projection_revision: 7,
  };
}

function snapshot(
  conversations: ConversationSummary[],
  viewRevision = 1,
): StateSnapshotPayload {
  return {
    creator_account_id: ACCOUNT_ID,
    view_revision: viewRevision,
    generated_at: AS_OF,
    conversations,
    analytics: {
      total_conversations: metric(conversations.length),
      total_messages: metric(conversations.filter(({ latest_message }) => latest_message).length),
      inbound_messages: metric(0),
      outbound_messages: metric(0),
    },
    coverage: {
      status: 'partial',
      phase: 'backfilling',
      generation_id: '90000000-0000-4000-8000-000000000001',
      as_of: AS_OF,
      discovered_conversations: conversations.length,
      complete_conversations: conversations.filter(
        ({ coverage }) => coverage.status === 'complete',
      ).length,
      complete_as_of: null,
      reason: 'conversation_evidence_missing',
    },
    projection: {
      status: 'current',
      canonical_revision: 7,
      projected_revision: 7,
      projected_at: AS_OF,
      reason: null,
    },
    live_freshness: {
      status: 'current',
      last_observed_at: AS_OF,
      last_committed_at: AS_OF,
      expires_at: '2026-07-18T13:02:00Z',
      pending_count: 0,
      reason: null,
    },
  };
}

function page(
  conversationId: string,
  items: MessageView[],
  {
    coverage = PARTIAL_COVERAGE,
    hasOlder = false,
    olderCursor = null,
  }: {
    coverage?: ConversationCoverage;
    hasOlder?: boolean;
    olderCursor?: string | null;
  } = {},
): MessagePageResponse {
  return {
    creator_account_id: ACCOUNT_ID,
    conversation_id: conversationId,
    projection_generation: 'projection-generation-7',
    read_revision: 7,
    generated_at: AS_OF,
    items,
    older_cursor: olderCursor,
    has_older_stored_items: hasOlder,
    conversation_coverage: coverage,
    projection: {
      status: 'current',
      canonical_revision: 7,
      projected_revision: 7,
      projected_at: AS_OF,
      reason: null,
    },
  };
}

function apiFor(
  pages: Record<string, MessagePageResponse>,
): MessageApi & { getPage: ReturnType<typeof vi.fn> } {
  const getPage = vi.fn(async ({ conversationId }: { conversationId: string }) => {
    const result = pages[conversationId];
    if (!result) throw new Error(`No test page for ${conversationId}`);
    return result;
  });
  return { getPage };
}

function renderInbox(
  store: ReturnType<typeof createBridgeTransportStore>,
  messageApi: MessageApi,
) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      <OperatorInboxView messageApi={messageApi} store={store} />
    </ThemeProvider>,
  );
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => cleanup());

describe('OperatorInboxView summary and REST-page integration', () => {
  it('renders loading state before the bounded state snapshot exists', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);

    renderInbox(store, apiFor({}));

    expect(screen.getByText('Loading conversations…')).toBeTruthy();
    expect(screen.getByText('Loading messages…')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders the empty state after a bounded snapshot with no conversations', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(snapshot([]));

    renderInbox(store, apiFor({}));

    expect(screen.getByText('No conversations to show')).toBeTruthy();
    expect(screen.queryByText(/read model|New chats will appear/iu)).toBeNull();
    expect(screen.getByText('Select a conversation')).toBeTruthy();
  });

  it('uses summary previews for the list and authenticated REST rows for the stream', async () => {
    const alicePreview = message(
      'alice-preview',
      'Alice summary preview',
      '2026-07-18T09:00:00Z',
    );
    const baileyPreview = message(
      'bailey-preview',
      'Bailey summary preview',
      '2026-07-18T10:00:00Z',
      'outbound',
    );
    const conversations = [
      conversation('alice', 'Alice North', alicePreview.sent_at, alicePreview),
      conversation('bailey', 'Bailey Hart', baileyPreview.sent_at, baileyPreview, 3),
    ];
    const messageApi = apiFor({
      bailey: page('bailey', [
        message('bailey-1', 'Hey creator', '2026-07-18T09:58:00Z'),
        message(
          'bailey-2',
          'Hello Bailey from REST',
          '2026-07-18T10:00:00Z',
          'outbound',
        ),
      ]),
    });
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(snapshot(conversations));

    renderInbox(store, messageApi);

    const conversationList = screen.getByRole('list', { name: 'Conversation list' });
    const conversationButtons = within(conversationList).getAllByRole('button');
    expect(conversationButtons[0].textContent).toContain('Bailey Hart');
    expect(conversationButtons[0].textContent).toContain('Bailey summary preview');
    expect(conversationButtons[1].textContent).toContain('Alice North');

    const stream = screen.getByRole('region', { name: 'Bailey Hart' });
    expect(await within(stream).findByText('Hey creator')).toBeTruthy();
    expect(within(stream).getByText('Hello Bailey from REST')).toBeTruthy();
    expect(within(stream).queryByText('Bailey summary preview')).toBeNull();
    expect(within(stream).getByRole('article', { name: 'Inbound message' })).toBeTruthy();
    expect(within(stream).getByRole('article', { name: 'Outbound message' })).toBeTruthy();
    expect(messageApi.getPage).toHaveBeenCalledWith(
      expect.objectContaining({ before: null, conversationId: 'bailey', limit: 50 }),
    );
  });

  it('applies a bounded tail delta without replacing the selected REST page', async () => {
    const alexPreview = message('alex-preview', 'Alex preview', '2026-07-18T12:00:00Z');
    const blairPreview = message('blair-preview', 'Blair preview', '2026-07-18T11:00:00Z');
    const alex = conversation('alex', 'Alex River', alexPreview.sent_at, alexPreview);
    const blair = conversation('blair', 'Blair Stone', blairPreview.sent_at, blairPreview);
    const messageApi = apiFor({
      alex: page('alex', [message('alex-1', 'Alex body', alexPreview.sent_at)]),
      blair: page('blair', [message('blair-1', 'Before delta', blairPreview.sent_at)]),
    });
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(snapshot([alex, blair]));
    renderInbox(store, messageApi);

    expect(await screen.findByText('Alex body')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Conversation with Blair Stone/ }));
    expect(await screen.findByText('Before delta')).toBeTruthy();

    const fresh = message(
      'blair-2',
      'Fresh bounded tail',
      '2026-07-18T13:00:00Z',
      'inbound',
      'positive',
    );
    act(() => {
      expect(
        store.applyDelta({
          creator_account_id: ACCOUNT_ID,
          view_revision: 2,
          committed_at: '2026-07-18T13:00:01Z',
          changes: [
            {
              type: 'conversation.upsert',
              conversation: conversation('blair', 'Blair Stone', fresh.sent_at, fresh, 1),
            },
            { type: 'message.tail.upsert', conversation_id: 'blair', message: fresh },
          ],
        }),
      ).toBe('applied');
    });

    const conversationList = screen.getByRole('list', { name: 'Conversation list' });
    expect(within(conversationList).getAllByRole('button')[0].textContent).toContain(
      'Blair Stone',
    );
    expect(within(conversationList).getByText('Fresh bounded tail')).toBeTruthy();
    const stream = screen.getByRole('region', { name: 'Blair Stone' });
    expect(within(stream).getByText('Before delta')).toBeTruthy();
    expect(within(stream).getByText('Fresh bounded tail')).toBeTruthy();
  });

  it('only claims start of available history when REST coverage proves the boundary', async () => {
    const preview = message('preview', 'Preview', AS_OF);
    const summary = conversation(
      'complete-chat',
      'Complete Chat',
      AS_OF,
      preview,
      0,
      COMPLETE_CONVERSATION_COVERAGE,
    );
    const messageApi = apiFor({
      'complete-chat': page(
        'complete-chat',
        [message('first', 'First available message', '2026-01-01T00:00:00Z')],
        { coverage: COMPLETE_CONVERSATION_COVERAGE },
      ),
    });
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(snapshot([summary]));

    renderInbox(store, messageApi);

    expect(await screen.findByText('First available message')).toBeTruthy();
    expect(screen.getByText('Start of conversation')).toBeTruthy();
    expect(screen.queryByText('No earlier messages on this computer')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Load earlier messages' })).toBeNull();
  });

  it('retains an already loaded REST page when live freshness degrades', async () => {
    const preview = message('casey-preview', 'Casey preview', AS_OF);
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(
      snapshot([conversation('casey', 'Casey Lane', preview.sent_at, preview)]),
    );
    renderInbox(
      store,
      apiFor({ casey: page('casey', [message('casey-1', 'Still visible', AS_OF)]) }),
    );

    expect(await screen.findByText('Still visible')).toBeTruthy();
    act(() => store.markDisconnected());

    expect(screen.queryByText('Updates paused')).toBeNull();
    expect(screen.queryByText('Connection interrupted')).toBeNull();
    expect(screen.getByText('Still visible')).toBeTruthy();
    await waitFor(() => expect(store.getState().liveFreshness.status).toBe('delayed'));
  });
});

describe('OperatorInboxView projection reason presentation', () => {
  it.each([
    'projection_missing',
    'projection_activation_pending',
    'projection_lag',
    'projection_degraded',
    'projection_exploded_unexpectedly',
  ])('states availability without inventing active work for %s', (reason) => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot({
      ...snapshot([]),
      projection: {
        status: 'unavailable',
        canonical_revision: 7,
        projected_revision: 0,
        projected_at: null,
        reason,
      },
    });

    renderInbox(store, apiFor({}));

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toBe('Conversations unavailable');
    expect(alert.textContent).not.toContain(reason);
    expect(screen.queryByRole('button', { name: /Check|Retry|Connect/iu })).toBeNull();
  });

  it('does not promise a completion time or automatic recovery for a cold read', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    const messageApi = apiFor({});
    store.beginResync();

    renderInbox(store, messageApi);

    expect(screen.getByRole('alert').textContent).toBe('Refreshing conversations');
    act(() => store.markDisconnected());
    expect(screen.getByRole('alert').textContent).toBe('Conversations unavailable');
    expect(messageApi.getPage).not.toHaveBeenCalled();
  });

  it('retains the loaded conversation through refresh and connected degradation without assuming a reconnect', async () => {
    const preview = message('retained-preview', 'Retained preview', AS_OF);
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.setConnection('connected');
    store.applySnapshot(snapshot([
      conversation('retained', 'Retained Fan', AS_OF, preview),
    ]));
    const messageApi = apiFor({
      retained: page('retained', [message('retained-body', 'Retained message', AS_OF)]),
    });
    renderInbox(store, messageApi);
    expect(await screen.findByText('Retained message')).toBeTruthy();

    act(() => store.beginResync());
    const refreshing = screen.getByRole('alert');
    expect(refreshing.textContent).toContain('Refreshing conversations');
    expect(refreshing.textContent).toContain('Showing your last conversations.');
    expect(screen.getByText('Retained message')).toBeTruthy();

    act(() => {
      store.markDisconnected();
      store.setConnection('connected');
    });
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Updates unavailable');
    expect(alert.textContent).toContain('Showing your last conversations.');
    expect(alert.textContent).not.toMatch(/reconnecting|paused|until|will/iu);
    expect(screen.getByText('Retained message')).toBeTruthy();
    expect(messageApi.getPage).toHaveBeenCalledTimes(1);
  });

  it.each(['pending', 'degraded'] as const)(
    'does not claim retained conversations for an empty %s projection',
    (status) => {
      const store = createBridgeTransportStore();
      store.bindAccount(ACCOUNT_ID);
      const ready = snapshot([]);
      store.applySnapshot({
        ...ready,
        coverage: { ...ready.coverage, status: 'complete', phase: 'complete' },
        projection: { ...ready.projection, status, projected_revision: 6, reason: 'projection_lag' },
      });
      renderInbox(store, apiFor({}));

      expect(screen.getByRole('alert').textContent).toBe(
        'Updates unavailable',
      );
      expect(screen.getByText('No conversations to show')).toBeTruthy();
    },
  );

  it.each(['pending', 'degraded'] as const)(
    'describes the actual retained conversation for a %s projection without claiming an update is running',
    async (status) => {
      const store = createBridgeTransportStore();
      store.bindAccount(ACCOUNT_ID);
      const preview = message('pending-preview', 'Pending preview', AS_OF);
      const ready = snapshot([conversation('pending', 'Pending Fan', AS_OF, preview)]);
      ready.coverage = {
        ...ready.coverage, status: 'complete', phase: 'complete', complete_conversations: 1,
        complete_as_of: AS_OF, reason: null,
      };
      store.applySnapshot(ready);
      const messageApi = apiFor({
        pending: page('pending', [message('pending-body', 'Retained conversation body', AS_OF)]),
      });
      renderInbox(store, messageApi);
      expect(await screen.findByText('Retained conversation body')).toBeTruthy();

      act(() => store.applySnapshot({
        ...ready,
        projection: { ...ready.projection, status, canonical_revision: 8, reason: 'projection_lag' },
      }));
      const alert = screen.getByRole('alert');
      expect(alert.textContent).toContain('Updates unavailable');
      expect(alert.textContent).toContain('Showing your last conversations.');
      expect(alert.textContent).not.toMatch(/being added|updating|will/iu);
      expect(screen.getByText('Retained conversation body')).toBeTruthy();
      expect(messageApi.getPage).toHaveBeenCalledTimes(1);
    },
  );

  it('does not claim retained conversations during refresh or degradation of an empty snapshot', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(snapshot([]));
    const messageApi = apiFor({});
    renderInbox(store, messageApi);

    act(() => store.beginResync());
    expect(screen.getByRole('alert').textContent).toBe('Refreshing conversations');
    expect(screen.getByText('No conversations to show')).toBeTruthy();
    act(() => {
      store.markDisconnected();
      store.setConnection('connected');
    });
    expect(screen.getByRole('alert').textContent).toBe('Updates unavailable');
    expect(screen.getByText('No conversations to show')).toBeTruthy();
    expect(messageApi.getPage).not.toHaveBeenCalled();
  });
});

describe('OperatorInboxView confirmed history phase', () => {
  it.each([
    ['blocked', 'configuration_not_applied', 'History incomplete', false],
    ['not_started', 'configuration_not_applied', 'History not started', false],
    ['paused', 'consent_revoked', 'Message history is off', false],
    ['not_started', 'consent_revoked', 'Message history is off', false],
    ['paused', 'history_sync_paused', 'History paused', true],
  ] as const)('describes %s / %s without an ineligible resume instruction', async (phase, reason, title, canResume) => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    const preview = message('history-preview', 'History preview', AS_OF);
    const ready = snapshot([conversation('history', 'History Fan', AS_OF, preview)]);
    store.applySnapshot({
      ...ready,
      coverage: { ...ready.coverage, phase, reason },
    });
    const messageApi = apiFor({
      history: page('history', [message('history-body', 'Available recent message', AS_OF)]),
    });
    renderInbox(store, messageApi);

    expect(await screen.findByText('Available recent message')).toBeTruthy();
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain(title);
    expect(alert.textContent).not.toMatch(/still syncing|Turn on|will/iu);
    expect(alert.textContent?.includes('Resume history sync in Settings.')).toBe(canResume);
    expect(messageApi.getPage).toHaveBeenCalledTimes(1);
  });
});
