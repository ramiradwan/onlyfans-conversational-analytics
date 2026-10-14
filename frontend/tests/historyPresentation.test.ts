import { describe, expect, it } from 'vitest';
import type { AgentStatePayload, HistoricalCoverage, HistorySettings } from '../src/protocol';
import { presentHistory } from '../src/utils/historyPresentation';

const settings: HistorySettings = {
  creator_account_id: 'creator-test', settings_revision: 1, consent_policy_version: 'test',
  consent_revision: 'test-consent', authorized_platform_creator_id: 'platform-test', desired_state: 'running',
  effective_state: 'running', effective_config_revision: 'test', recent_window_days: 30,
  page_size: 50, pages_per_wake: 2, request_interval_ms: 1000, retry_limit: 3,
  updated_at: '2026-10-14T00:00:00Z',
};
const coverage: HistoricalCoverage = {
  status: 'partial', phase: 'not_started', generation_id: null, as_of: null,
  discovered_conversations: 0, complete_conversations: 0, complete_as_of: null, reason: null,
};
const agent: AgentStatePayload = {
  creator_account_id: 'creator-test', status: 'connected',
  agent_installation_id: '11111111-1111-4111-8111-111111111111',
  connection_id: '22222222-2222-4222-8222-222222222222',
  required_config_revision: 'test', applied_config_revision: 'test',
  required_history_settings_revision: 1, applied_history_settings_revision: 1,
  last_heartbeat_at: '2026-10-14T00:00:00Z', degraded_reason: null,
  browser: { capture: 'active', site_access: 'granted', history_permission: 'granted',
    history_error_code: 'unsupported_revision', legal_review_required: false, reported_at: '2026-10-14T00:00:00Z' },
};

describe('owner-scoped acquisition and saved history', () => {
  it('keeps a parameter dependency separate from connection, permission and saved data', () => {
    const waiting = { ...agent, browser: { ...agent.browser!, history_error_code: 'history_dependency_wait' as const } };
    expect(presentHistory(settings, waiting, coverage)).toMatchObject({ label: 'Waiting', tone: 'info' });
    expect(presentHistory(settings, waiting, { ...coverage, status: 'complete' }).label).toBe('Saved');
    const incompatible = { ...agent, browser: { ...agent.browser!, history_error_code: 'software_update_required' as const } };
    expect(presentHistory(settings, incompatible, coverage)).toMatchObject({ label: 'Update needed',
      detail: 'History collection is waiting for a compatible app update.' });
  });
  it('distinguishes unsupported signing from permission and saved coverage', () => {
    expect(presentHistory(settings, agent, coverage)).toMatchObject({ label: 'Needs attention', tone: 'warning' });
    expect(presentHistory(settings, { ...agent, browser: { ...agent.browser!, history_permission: 'missing' } }, coverage).label).toBe('Permission needed');
    expect(presentHistory(settings, agent, { ...coverage, status: 'complete', phase: 'complete',
      discovered_conversations: 11, complete_conversations: 11 })).toMatchObject({ label: 'Saved', tone: 'success' });
  });

  it('does not infer permission refusal or a signing failure from missing observations', () => {
    expect(presentHistory(settings, { ...agent, browser: null }, coverage).label).toBe('Waiting');
    expect(presentHistory(settings, null, coverage).label).toBe('Not connected');
    expect(presentHistory(settings, null, { ...coverage, status: 'complete' }).label).toBe('Saved');
  });
});
