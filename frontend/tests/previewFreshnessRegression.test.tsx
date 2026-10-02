import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { ChatListPane } from '../src/components/inbox/ChatListPane';
import { AppAppBar, getStatusPresentation } from '../src/layouts/AppAppBar';
import type { ConversationRecord } from '../src/protocol';
import { bridgeTransportStore, createBridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
it('renders a sanitized text preview with normalized block boundaries', () => {
  const conversation = { conversation_id: 'test', platform_user_id: 'test', display_name: 'Example', unread_count: 0,
    last_message_at: null, latest_message: { message_id: 'test', text: '<p>Hello &amp; welcome</p><p>Next<br>line</p><script>hidden()</script>', sent_at: '2026-10-02T12:00:00Z', direction: 'inbound', sentiment: 'neutral' },
    coverage: { status: 'unknown', boundary: null, earliest_available_at: null, latest_acquired_at: null, data_as_of: null, reason_code: null },
  } as ConversationRecord;
  const view = render(<ThemeProvider theme={theme}><ChatListPane conversations={[conversation]} isLoading={false} selectedConversationId={null} onSelectConversation={() => {}} /></ThemeProvider>);
  expect(view.getByText('Hello & welcome Next line')).toBeTruthy();
  expect(view.container.textContent).not.toContain('<p>');
  expect(view.container.textContent).not.toContain('hidden()');
  expect(view.container.querySelector('script')).toBeNull();
});
it('does not claim current messages from passive freshness', () => {
  const state = createBridgeTransportStore().getState();
  const current = { ...state, connection: 'connected' as const, viewRevision: 1, readModelState: 'realtime' as const,
    coverage: { ...state.coverage, status: 'complete' as const, phase: 'complete' as const },
    projection: { ...state.projection, status: 'current' as const },
    liveFreshness: { ...state.liveFreshness, status: 'current' as const },
    agent: { status: 'connected', required_config_revision: 'a', applied_config_revision: 'a', required_history_settings_revision: 1, applied_history_settings_revision: 1, degraded_reason: null } as typeof state.agent,
  };
  expect(getStatusPresentation(current).label).toBe('Messages not checked');
  vi.spyOn(bridgeTransportStore, 'getState').mockReturnValue(current);
  const view = render(<ThemeProvider theme={theme}><AppAppBar onDrawerToggle={() => {}} /></ThemeProvider>);
  expect(view.getAllByRole('button', { name: 'Status: Messages not checked. Show details' })).toHaveLength(2);
  expect(view.queryAllByRole('button', { name: 'Status: Up to date. Show details' })).toHaveLength(0);
});
