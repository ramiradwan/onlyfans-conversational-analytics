import { ThemeProvider } from '@mui/material/styles';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type {
  ConversationView,
  MessageView,
  StateSnapshotPayload,
} from '../src/protocol';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';
import OperatorInboxView from '../src/views/OperatorInboxView';

const ACCOUNT_ID = 'creator-account';

function message(
  messageId: string,
  text: string,
  sentAt: string,
  direction: MessageView['direction'] = 'inbound',
  sentiment: MessageView['sentiment'] = 'neutral',
): MessageView {
  return {
    message_id: messageId,
    text,
    sent_at: sentAt,
    direction,
    sentiment,
  };
}

function conversation(
  conversationId: string,
  displayName: string,
  lastMessageAt: string,
  messages: MessageView[],
  unreadCount = 0,
): ConversationView {
  return {
    conversation_id: conversationId,
    platform_user_id: 'fan-' + conversationId,
    display_name: displayName,
    unread_count: unreadCount,
    last_message_at: lastMessageAt,
    messages,
  };
}

function snapshot(
  conversations: ConversationView[],
  viewRevision = 1,
): StateSnapshotPayload {
  return {
    creator_account_id: ACCOUNT_ID,
    view_revision: viewRevision,
    generated_at: '2026-07-18T12:00:00.000Z',
    conversations,
    analytics: {
      total_conversations: conversations.length,
      total_messages: conversations.reduce(
        (total, current) => total + current.messages.length,
        0,
      ),
      inbound_messages: conversations.reduce(
        (total, current) =>
          total +
          current.messages.filter((currentMessage) => currentMessage.direction === 'inbound')
            .length,
        0,
      ),
      outbound_messages: conversations.reduce(
        (total, current) =>
          total +
          current.messages.filter((currentMessage) => currentMessage.direction === 'outbound')
            .length,
        0,
      ),
    },
  };
}

function renderInbox(store: ReturnType<typeof createBridgeTransportStore>) {
  return render(
    <ThemeProvider theme={theme} defaultMode="light">
      <OperatorInboxView store={store} />
    </ThemeProvider>,
  );
}

afterEach(() => cleanup());

describe('OperatorInboxView transportStore projection', () => {
  it('renders the loading state before a snapshot exists', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);

    renderInbox(store);

    expect(screen.getByText('Loading conversations…')).toBeTruthy();
    expect(screen.getByText('Loading messages…')).toBeTruthy();
    expect(screen.getByText('Connecting')).toBeTruthy();
  });

  it('renders the empty state after an empty snapshot', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(snapshot([]));

    renderInbox(store);

    expect(screen.getByText('No conversations yet')).toBeTruthy();
    expect(screen.getByText('Select a conversation')).toBeTruthy();
  });

  it('renders recency-sorted chats and direction-aware messages from a snapshot', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(
      snapshot([
        conversation(
          'alice',
          'Alice North',
          '2026-07-18T09:00:00.000Z',
          [message('alice-1', 'Older hello', '2026-07-18T09:00:00.000Z')],
        ),
        conversation(
          'bailey',
          'Bailey Hart',
          '2026-07-18T10:00:00.000Z',
          [
            message(
              'bailey-1',
              'Hey creator',
              '2026-07-18T09:58:00.000Z',
              'inbound',
              'positive',
            ),
            message(
              'bailey-2',
              'Hello Bailey',
              '2026-07-18T10:00:00.000Z',
              'outbound',
              'positive',
            ),
          ],
          3,
        ),
      ]),
    );

    renderInbox(store);

    const conversationList = screen.getByRole('list', { name: 'Conversation list' });
    const conversationButtons = within(conversationList).getAllByRole('button');
    expect(conversationButtons[0].textContent).toContain('Bailey Hart');
    expect(conversationButtons[1].textContent).toContain('Alice North');

    const stream = screen.getByRole('region', { name: 'Bailey Hart' });
    expect(within(stream).getByText('Hey creator')).toBeTruthy();
    expect(within(stream).getByText('Hello Bailey')).toBeTruthy();
    expect(within(stream).getByRole('article', { name: 'Inbound message' })).toBeTruthy();
    expect(within(stream).getByRole('article', { name: 'Outbound message' })).toBeTruthy();
  });

  it('updates both panes from a delta while preserving the selected chat', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    const blairInitial = conversation(
      'blair',
      'Blair Stone',
      '2026-07-18T11:00:00.000Z',
      [message('blair-1', 'Before delta', '2026-07-18T11:00:00.000Z')],
    );
    store.applySnapshot(
      snapshot([
        conversation(
          'alex',
          'Alex River',
          '2026-07-18T12:00:00.000Z',
          [message('alex-1', 'Most recent', '2026-07-18T12:00:00.000Z')],
        ),
        blairInitial,
      ]),
    );
    renderInbox(store);

    fireEvent.click(screen.getByRole('button', { name: /Conversation with Blair Stone/ }));
    expect(screen.getByRole('region', { name: 'Blair Stone' })).toBeTruthy();

    const blairUpdated = conversation(
      'blair',
      'Blair Stone',
      '2026-07-18T13:00:00.000Z',
      [
        ...blairInitial.messages,
        message(
          'blair-2',
          'Fresh from delta',
          '2026-07-18T13:00:00.000Z',
          'inbound',
          'positive',
        ),
      ],
      1,
    );
    act(() => {
      expect(
        store.applyDelta({
          creator_account_id: ACCOUNT_ID,
          view_revision: 2,
          committed_at: '2026-07-18T13:00:01.000Z',
          changes: [{ type: 'conversation.upsert', conversation: blairUpdated }],
        }),
      ).toBe('applied');
    });

    const conversationList = screen.getByRole('list', { name: 'Conversation list' });
    expect(within(conversationList).getAllByRole('button')[0].textContent).toContain(
      'Blair Stone',
    );
    expect(within(conversationList).getByText('Fresh from delta')).toBeTruthy();

    const stream = screen.getByRole('region', { name: 'Blair Stone' });
    expect(within(stream).getByText('Before delta')).toBeTruthy();
    expect(within(stream).getByText('Fresh from delta')).toBeTruthy();
  });

  it('visibly marks degraded state while retaining the latest snapshot', () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(
      snapshot([
        conversation(
          'casey',
          'Casey Lane',
          '2026-07-18T10:00:00.000Z',
          [message('casey-1', 'Still visible', '2026-07-18T10:00:00.000Z')],
        ),
      ]),
    );
    renderInbox(store);

    act(() => store.markDisconnected());

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Realtime updates paused');
    expect(alert.textContent).toContain('Showing cached data');
    expect(screen.getByText('Degraded')).toBeTruthy();
    const stream = screen.getByRole('region', { name: 'Casey Lane' });
    expect(stream).toBeTruthy();
    expect(within(stream).getByText('Still visible')).toBeTruthy();
  });
});
