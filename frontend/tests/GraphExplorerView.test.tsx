import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { bridgeTransportStore } from '../src/store/transportStore';
import { theme } from '../src/theme';
import GraphExplorerView from '../src/views/GraphExplorerView';

afterEach(() => {
  cleanup();
  bridgeTransportStore.reset();
});

describe('GraphExplorerView projection truthfulness', () => {
  it('shows unavailable projection state without generated or sample results', () => {
    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <GraphExplorerView />
      </ThemeProvider>,
    );

    expect(screen.getByRole('heading', { name: 'Graph explorer' })).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Graph data is not ready');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByText(/AliceFan|BobSubscriber|engagement score/i)).toBeNull();
  });

  it('states the authenticated-query limitation when the local projection is current', () => {
    bridgeTransportStore.bindAccount('creator-1');
    bridgeTransportStore.applySnapshot({
      creator_account_id: 'creator-1',
      view_revision: 1,
      generated_at: '2026-07-19T12:00:00Z',
      conversations: [],
      analytics: Object.fromEntries(
        ['total_conversations', 'total_messages', 'inbound_messages', 'outbound_messages'].map(
          (name) => [
            name,
            {
              value: 0,
              basis: 'complete',
              observed_range: { start: null, end: null },
              complete_range: { start: null, end: null },
              sample_size: 0,
              as_of: '2026-07-19T12:00:00Z',
              projection_revision: 2,
            },
          ],
        ),
      ),
      coverage: {
        status: 'complete',
        phase: 'complete',
        generation_id: '90000000-0000-4000-8000-000000000001',
        as_of: '2026-07-19T12:00:00Z',
        discovered_conversations: 0,
        complete_conversations: 0,
        complete_as_of: '2026-07-19T12:00:00Z',
        reason: null,
      },
      projection: {
        status: 'current',
        canonical_revision: 2,
        projected_revision: 2,
        projected_at: '2026-07-19T12:00:00Z',
        reason: null,
      },
      live_freshness: {
        status: 'current',
        last_observed_at: '2026-07-19T12:00:00Z',
        last_committed_at: '2026-07-19T12:00:00Z',
        expires_at: '2026-07-19T12:02:00Z',
        pending_count: 0,
        reason: null,
      },
    });

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <GraphExplorerView />
      </ThemeProvider>,
    );

    expect(screen.getByRole('alert').textContent).toContain(
      'Interactive graph queries are not enabled in this Beta',
    );
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('replaces a raw projection reason code with friendly text', () => {
    bridgeTransportStore.bindAccount('creator-1');
    bridgeTransportStore.applySnapshot({
      creator_account_id: 'creator-1',
      view_revision: 1,
      generated_at: '2026-07-19T12:00:00Z',
      conversations: [],
      analytics: Object.fromEntries(
        ['total_conversations', 'total_messages', 'inbound_messages', 'outbound_messages'].map(
          (name) => [
            name,
            {
              value: 0,
              basis: 'complete',
              observed_range: { start: null, end: null },
              complete_range: { start: null, end: null },
              sample_size: 0,
              as_of: '2026-07-19T12:00:00Z',
              projection_revision: 2,
            },
          ],
        ),
      ),
      coverage: {
        status: 'complete',
        phase: 'complete',
        generation_id: '90000000-0000-4000-8000-000000000001',
        as_of: '2026-07-19T12:00:00Z',
        discovered_conversations: 0,
        complete_conversations: 0,
        complete_as_of: '2026-07-19T12:00:00Z',
        reason: null,
      },
      projection: {
        status: 'degraded',
        canonical_revision: 2,
        projected_revision: 1,
        projected_at: '2026-07-19T12:00:00Z',
        reason: 'projection_degraded',
      },
      live_freshness: {
        status: 'current',
        last_observed_at: '2026-07-19T12:00:00Z',
        last_committed_at: '2026-07-19T12:00:00Z',
        expires_at: '2026-07-19T12:02:00Z',
        pending_count: 0,
        reason: null,
      },
    });

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <GraphExplorerView />
      </ThemeProvider>,
    );

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Conversation data needs attention');
    expect(alert.textContent).not.toContain('projection_degraded');
  });
});
