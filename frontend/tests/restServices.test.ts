import { describe, expect, it, vi } from 'vitest';

import { createHistorySettingsApi } from '../src/services/historySettingsApi';
import { createMessageApi, StaleMessageCursorError } from '../src/services/messageApi';

const projection = {
  status: 'current',
  canonical_revision: 3,
  projected_revision: 3,
  projected_at: '2026-07-19T12:00:00Z',
  reason: null,
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const historySettings = {
  creator_account_id: 'creator-1',
  settings_revision: 4,
  consent_policy_version: 'history-consent-v1',
  consent_revision: null,
  authorized_platform_creator_id: null,
  desired_state: 'not_started',
  effective_state: 'not_applied',
  effective_config_revision: null,
  recent_window_days: 30,
  page_size: 50,
  pages_per_wake: 2,
  request_interval_ms: 1000,
  retry_limit: 3,
  updated_at: '2026-07-19T12:00:00Z',
};

describe('revision-bound REST services', () => {
  it('pages messages with an opaque cursor and same-origin credentials', async () => {
    const request = vi.fn(async () =>
      json({
        creator_account_id: 'creator-1',
        conversation_id: 'chat/one',
        projection_generation: 'generation-1',
        read_revision: 3,
        generated_at: '2026-07-19T12:00:00Z',
        items: [],
        older_cursor: null,
        has_older_stored_items: false,
        conversation_coverage: {
          status: 'complete',
          boundary: 'history_start',
          earliest_available_at: null,
          latest_acquired_at: null,
          data_as_of: '2026-07-19T12:00:00Z',
          reason_code: null,
        },
        projection,
      }),
    );
    const api = createMessageApi({ fetch: request as typeof fetch });

    const result = await api.getPage({
      conversationId: 'chat/one',
      before: 'opaque+/=',
      limit: 25,
    });

    const [url, init] = request.mock.calls[0];
    expect(String(url)).toContain('/api/v1/conversations/chat%2Fone/messages');
    expect(new URL(String(url)).searchParams.get('before')).toBe('opaque+/=');
    expect(new URL(String(url)).searchParams.get('limit')).toBe('25');
    expect(init?.credentials).toBe('same-origin');
    expect(result.creator_account_id).toBe('creator-1');
    expect(result.conversation_id).toBe('chat/one');
    expect(result.projection_generation).toBe('generation-1');
    expect(result.read_revision).toBe(3);
    expect(result.older_cursor).toBeNull();
    expect(result.has_older_stored_items).toBe(false);
  });

  it('classifies a projection-generation cursor conflict as a stale paging window', async () => {
    const api = createMessageApi({
      fetch: vi.fn(async () => new Response(null, { status: 409 })) as typeof fetch,
    });
    await expect(api.getPage({ conversationId: 'chat-1' })).rejects.toBeInstanceOf(
      StaleMessageCursorError,
    );
  });

  it('uses If-Match, CSRF, exact settings paths, and same-origin credentials', async () => {
    const request = vi.fn(async () => json(historySettings));
    const api = createHistorySettingsApi({
      fetch: request as typeof fetch,
      getCsrfToken: () => 'csrf-token',
    });

    await api.get();
    await api.update(4, {
      desired_state: 'running',
      consent_policy_version: 'history-consent-v1',
      accept_consent: true,
      recent_window_days: 30,
      page_size: 50,
      pages_per_wake: 2,
      request_interval_ms: 1000,
      retry_limit: 3,
    });
    await api.revoke(4);

    expect(request.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/settings/history',
      '/api/v1/settings/history',
      '/api/v1/settings/history/consent',
    ]);
    expect(request.mock.calls.map(([, init]) => init?.method)).toEqual(['GET', 'PUT', 'DELETE']);
    const putHeaders = request.mock.calls[1][1]?.headers as Record<string, string>;
    expect(putHeaders['If-Match']).toBe('4');
    expect(putHeaders['X-CSRF-Token']).toBe('csrf-token');
    expect(request.mock.calls[1][1]?.credentials).toBe('same-origin');
  });
});
