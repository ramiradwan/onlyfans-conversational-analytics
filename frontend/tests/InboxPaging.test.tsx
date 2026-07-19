import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  AnalyticsMetric,
  MessagePageResponse,
  StateSnapshotPayload,
} from '../src/protocol';
import type { MessageApi } from '../src/services/messageApi';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';
import OperatorInboxView from '../src/views/OperatorInboxView';

const projection = {
  status: 'current' as const,
  canonical_revision: 3,
  projected_revision: 3,
  projected_at: '2026-07-19T12:00:00Z',
  reason: null,
};

const conversationCoverage = {
  status: 'partial' as const,
  boundary: null,
  earliest_available_at: null,
  latest_acquired_at: '2026-07-19T12:00:00Z',
  data_as_of: '2026-07-19T12:00:00Z',
  reason_code: null,
};

function page(items: MessagePageResponse['items'], olderCursor: string | null): MessagePageResponse {
  return {
    creator_account_id: 'creator-1',
    conversation_id: 'conversation-1',
    projection_generation: 'generation-1',
    read_revision: 3,
    generated_at: '2026-07-19T12:00:00Z',
    items,
    older_cursor: olderCursor,
    has_older_stored_items: olderCursor !== null,
    conversation_coverage: conversationCoverage,
    projection,
  };
}

function metric(value: number): AnalyticsMetric {
  return {
    value,
    basis: 'synced_subset',
    observed_range: {
      start: '2026-07-18T12:00:00Z',
      end: '2026-07-19T12:00:00Z',
    },
    complete_range: null,
    sample_size: 2,
    as_of: '2026-07-19T12:00:00Z',
    projection_revision: 3,
  };
}

function snapshot(): StateSnapshotPayload {
  return {
    creator_account_id: 'creator-1',
    view_revision: 3,
    generated_at: '2026-07-19T12:00:00Z',
    conversations: [
      {
        conversation_id: 'conversation-1',
        platform_user_id: 'fan-1',
        display_name: 'Fan One',
        unread_count: 0,
        last_message_at: '2026-07-19T12:00:00Z',
        latest_message: {
          message_id: 'new-2',
          text: 'Newest',
          sent_at: '2026-07-19T12:00:00Z',
          direction: 'inbound',
          sentiment: 'unknown',
        },
        coverage: conversationCoverage,
      },
    ],
    analytics: {
      total_conversations: metric(1),
      total_messages: metric(2),
      inbound_messages: metric(2),
      outbound_messages: metric(0),
    },
    coverage: {
      status: 'partial',
      phase: 'backfilling',
      generation_id: '10000000-0000-4000-8000-000000000001',
      as_of: '2026-07-19T12:00:00Z',
      discovered_conversations: 1,
      complete_conversations: 0,
      complete_as_of: null,
      reason: null,
    },
    projection,
    live_freshness: {
      status: 'current',
      last_observed_at: '2026-07-19T12:00:00Z',
      last_committed_at: '2026-07-19T12:00:00Z',
      expires_at: '2026-07-19T12:02:00Z',
      pending_count: 0,
      reason: null,
    },
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Inbox REST paging', () => {
  it('does not abort its initial request when publishing the loading state', async () => {
    let resolvePage: ((value: MessagePageResponse) => void) | null = null;
    let requestSignal: AbortSignal | undefined;
    const getPage = vi.fn(({ signal }: { signal?: AbortSignal }) => {
      requestSignal = signal;
      return new Promise<MessagePageResponse>((resolve) => {
        resolvePage = resolve;
      });
    });
    const store = createBridgeTransportStore();
    store.bindAccount('creator-1');
    store.applySnapshot(snapshot());

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <OperatorInboxView messageApi={{ getPage }} store={store} />
      </ThemeProvider>,
    );

    await waitFor(() => expect(getPage).toHaveBeenCalledTimes(1));
    expect(requestSignal?.aborted).toBe(false);
    await act(async () => {
      resolvePage?.(page([], null));
      await Promise.resolve();
    });
    expect(requestSignal?.aborted).toBe(false);
    expect(screen.getByText('No stored messages yet')).toBeTruthy();
  });

  it('retries one errored page after the readiness key changes without looping', async () => {
    const getPage = vi.fn(async () => {
      throw new Error('Brain was restarting');
    });
    const store = createBridgeTransportStore();
    store.bindAccount('creator-1');
    store.applySnapshot(snapshot());

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <OperatorInboxView messageApi={{ getPage }} store={store} />
      </ThemeProvider>,
    );

    expect(await screen.findByRole('button', { name: 'Try again' })).toBeTruthy();
    await waitFor(() => expect(getPage).toHaveBeenCalledTimes(1));

    act(() => {
      store.setAgent({
        creator_account_id: 'creator-1',
        status: 'connected',
        agent_installation_id: '10000000-0000-4000-8000-000000000001',
        connection_id: '20000000-0000-4000-8000-000000000002',
        required_config_revision: 'config-8',
        applied_config_revision: 'config-8',
        required_history_settings_revision: 0,
        applied_history_settings_revision: 0,
        last_heartbeat_at: '2026-07-19T12:00:01Z',
        degraded_reason: null,
      });
    });

    await waitFor(() => expect(getPage).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('button', { name: 'Try again' })).toBeTruthy();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    expect(getPage).toHaveBeenCalledTimes(2);

    act(() => {
      store.setAgent({
        ...store.getState().agent!,
        last_heartbeat_at: '2026-07-19T12:00:02Z',
      });
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    expect(getPage).toHaveBeenCalledTimes(2);
  });

  it('loads the latest bounded window then prepends an older page without losing current rows', async () => {
    let olderResolved = false;
    const rect = (top: number, height: number): DOMRect => ({
      bottom: top + height,
      height,
      left: 0,
      right: 320,
      top,
      width: 320,
      x: 0,
      y: top,
      toJSON: () => ({}),
    });
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
      if (this.dataset.messageScroll === 'true') return rect(100, 400);
      if (this.dataset.messageId === 'new-1') return rect(olderResolved ? 180 : 120, 40);
      if (this.dataset.messageId === 'old-1') return rect(120, 40);
      return rect(0, 0);
    });

    const latest = page(
      [
        {
          message_id: 'new-1',
          text: 'Recent one',
          sent_at: '2026-07-19T11:59:00Z',
          direction: 'inbound',
          sentiment: 'unknown',
        },
        {
          message_id: 'new-2',
          text: 'Newest',
          sent_at: '2026-07-19T12:00:00Z',
          direction: 'inbound',
          sentiment: 'unknown',
        },
      ],
      'cursor-older',
    );
    const older = page(
      [
        {
          message_id: 'old-1',
          text: 'Earlier message',
          sent_at: '2026-07-18T12:00:00Z',
          direction: 'outbound',
          sentiment: 'unknown',
        },
      ],
      null,
    );
    const getPage = vi.fn(async ({ before }: { before?: string | null }) => {
      if (before !== 'cursor-older') return latest;
      olderResolved = true;
      return older;
    });
    const api: MessageApi = { getPage };
    const store = createBridgeTransportStore();
    store.bindAccount('creator-1');
    store.applySnapshot(snapshot());

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <OperatorInboxView messageApi={api} store={store} />
      </ThemeProvider>,
    );

    const stream = await screen.findByRole('region', { name: 'Fan One' });
    expect(await within(stream).findByText('Newest')).toBeTruthy();
    const scrollContainer = stream.querySelector<HTMLElement>('[data-message-scroll="true"]');
    expect(scrollContainer).not.toBeNull();
    expect(scrollContainer?.hasAttribute('aria-live')).toBe(false);
    expect(stream.querySelector('[role="status"][aria-live="polite"]')).not.toBeNull();
    const loadEarlier = screen.getByRole('button', { name: 'Load earlier messages' });
    loadEarlier.focus();
    expect(document.activeElement).toBe(loadEarlier);
    fireEvent.click(loadEarlier);
    expect(await screen.findByText('Earlier message')).toBeTruthy();
    expect(within(stream).getByText('Newest')).toBeTruthy();
    await waitFor(() => expect(getPage).toHaveBeenCalledTimes(2));
    expect(getPage.mock.calls[1][0].before).toBe('cursor-older');
    expect(scrollContainer?.scrollTop).toBe(60);
    expect(document.activeElement).toBe(loadEarlier);
  });
});
