import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AnalyticsMetric,
  ConversationCoverage,
  ConversationSummary,
  MessagePageResponse,
  StateSnapshotPayload,
} from '../src/protocol';
import type { MessageApi } from '../src/services/messageApi';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';
import OperatorInboxView from '../src/views/OperatorInboxView';

const ACCOUNT_ID = 'creator-account';
const AS_OF = '2026-09-17T10:00:00Z';

function installMatchMedia(width: number) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn((query: string) => {
      const minimum = query.match(/min-width:\s*([\d.]+)px/);
      const maximum = query.match(/max-width:\s*([\d.]+)px/);
      const matches = (minimum === null || width >= Number(minimum[1]))
        && (maximum === null || width <= Number(maximum[1]));
      return {
        matches,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      } as MediaQueryList;
    }),
  });
}

function metric(value: number): AnalyticsMetric {
  return {
    value,
    basis: 'complete',
    observed_range: { start: '2026-01-01T00:00:00Z', end: AS_OF },
    complete_range: { start: '2026-01-01T00:00:00Z', end: AS_OF },
    sample_size: value,
    as_of: AS_OF,
    projection_revision: 7,
  };
}

const coverage: ConversationCoverage = {
  status: 'complete',
  boundary: 'history_start',
  earliest_available_at: '2026-01-01T00:00:00Z',
  latest_acquired_at: AS_OF,
  data_as_of: AS_OF,
  reason_code: null,
};

const conversation: ConversationSummary = {
  conversation_id: 'casey',
  platform_user_id: 'fan-casey',
  display_name: 'Casey Lane',
  unread_count: 1,
  last_message_at: AS_OF,
  latest_message: {
    message_id: 'casey-preview',
    text: 'Latest preview',
    sent_at: AS_OF,
    direction: 'inbound',
    sentiment: 'neutral',
  },
  coverage,
};

const snapshot: StateSnapshotPayload = {
  creator_account_id: ACCOUNT_ID,
  view_revision: 7,
  generated_at: AS_OF,
  conversations: [conversation],
  analytics: {
    total_conversations: metric(1),
    total_messages: metric(1),
    inbound_messages: metric(1),
    outbound_messages: metric(0),
  },
  coverage: {
    status: 'complete',
    phase: 'complete',
    generation_id: '90000000-0000-4000-8000-000000000001',
    as_of: AS_OF,
    discovered_conversations: 1,
    complete_conversations: 1,
    complete_as_of: AS_OF,
    reason: null,
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
    expires_at: '2026-09-17T10:02:00Z',
    pending_count: 0,
    reason: null,
  },
};

const page: MessagePageResponse = {
  creator_account_id: ACCOUNT_ID,
  conversation_id: conversation.conversation_id,
  projection_generation: 'projection-generation-7',
  read_revision: 7,
  generated_at: AS_OF,
  items: [{
    message_id: 'casey-1',
    text: 'Hello from the selected conversation',
    sent_at: AS_OF,
    direction: 'inbound',
    sentiment: 'neutral',
  }],
  older_cursor: null,
  has_older_stored_items: false,
  conversation_coverage: coverage,
  projection: snapshot.projection,
};

beforeEach(() => {
  installMatchMedia(820);
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => cleanup());

describe('OperatorInboxView responsive master-detail flow', () => {
  it('uses list/detail navigation below the md two-pane breakpoint and restores focus on Back', async () => {
    const store = createBridgeTransportStore();
    store.bindAccount(ACCOUNT_ID);
    store.applySnapshot(snapshot);
    store.setAgent({
      creator_account_id: ACCOUNT_ID,
      status: 'connected',
      agent_installation_id: '90000000-0000-4000-8000-000000000002',
      connection_id: '90000000-0000-4000-8000-000000000003',
      required_config_revision: 'r1',
      applied_config_revision: 'r1',
      required_history_settings_revision: 1,
      applied_history_settings_revision: 1,
      last_heartbeat_at: AS_OF,
      degraded_reason: null,
    });
    const messageApi: MessageApi = { getPage: vi.fn(async () => page) };

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <OperatorInboxView messageApi={messageApi} store={store} />
      </ThemeProvider>,
    );

    const list = screen.getByRole('list', { name: 'Conversation list' });
    const conversationButton = within(list).getByRole('button', { name: /Conversation with Casey Lane/ });
    expect(screen.queryByRole('region', { name: 'Casey Lane' })).toBeNull();

    fireEvent.click(conversationButton);

    const back = await screen.findByRole('button', { name: 'Back to conversations' });
    await waitFor(() => expect(document.activeElement).toBe(back));
    const stream = screen.getByRole('region', { name: 'Casey Lane' });
    expect(await within(stream).findByText('Hello from the selected conversation')).toBeTruthy();
    expect(screen.queryByRole('list', { name: 'Conversation list' })).toBeNull();

    fireEvent.click(back);

    const restored = await screen.findByRole('button', { name: /Conversation with Casey Lane/ });
    await waitFor(() => expect(document.activeElement).toBe(restored));
    expect(screen.queryByRole('button', { name: 'Back to conversations' })).toBeNull();
    expect(screen.getByRole('list', { name: 'Conversation list' })).toBeTruthy();
  });
});
