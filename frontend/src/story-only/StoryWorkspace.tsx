/** STORY ONLY: production shell and views composed with deterministic journey fixtures. */
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { storyAnalyticsState, storyDateRange } from './analyticsFixtures';
import type { AnalyticsReadState } from '../analytics';
import { AnalyticsPresentation } from '../components/analytics';
import { AppShell } from '../layouts/AppShell';
import type {
  AgentStatePayload,
  AnalyticsMetric,
  ConversationSummary,
  HistorySettings,
  MessagePageResponse,
  StateSnapshotPayload,
} from '../protocol';
import type { CapabilityLicenseApi } from '../services/capabilityLicenseApi';
import type {
  CompanionPairingApi,
  CompanionPairingStatus,
} from '../services/companionPairingApi';
import type { CreatorVaultApi, CreatorVaultStatus } from '../services/creatorVaultApi';
import type { HistorySettingsApi } from '../services/historySettingsApi';
import type { MessageApi } from '../services/messageApi';
import type { WebAuthnApi } from '../services/webauthnApi';
import { bridgeTransportStore } from '../store/transportStore';
import { useUserStore } from '../store/userStore';
import CreatorDashboardView from '../views/CreatorDashboardView';
import OperatorInboxView from '../views/OperatorInboxView';
import SettingsWithVaultView from '../views/SettingsWithVaultView';
import { WebAuthnAccessView } from '../views/WebAuthnAccessView';

export type StoryWorkspaceName = 'home' | 'analytics' | 'inbox' | 'settings' | 'passkey';
export type StoryJourneyName = 'loading' | 'fresh' | 'syncing' | 'populated';

export const storyWorkspaceOptions: readonly { key: StoryWorkspaceName; label: string }[] = [
  { key: 'home', label: 'Dashboard' },
  { key: 'analytics', label: 'Analytics' },
  { key: 'inbox', label: 'Inbox' },
  { key: 'settings', label: 'Settings' },
  { key: 'passkey', label: 'Passkey sign-in' },
];

export const storyJourneyOptions: readonly { key: StoryJourneyName; label: string }[] = [
  { key: 'loading', label: 'Loading' },
  { key: 'fresh', label: 'Fresh install' },
  { key: 'syncing', label: 'History syncing' },
  { key: 'populated', label: 'Populated' },
];

const ACCOUNT = 'story-only-account';
const AS_OF = '2026-06-30T12:00:00.000Z';

function metric(value: number, partial: boolean): AnalyticsMetric {
  return {
    value,
    basis: partial ? 'synced_subset' : 'complete',
    observed_range: { start: '2026-01-04T09:00:00.000Z', end: AS_OF },
    complete_range: partial ? null : { start: '2026-01-04T09:00:00.000Z', end: AS_OF },
    sample_size: value,
    as_of: AS_OF,
    projection_revision: 42,
  };
}

const FANS = [
  ['story-fan-1', 'Riley Morgan', 'Can you send the full set from Tuesday?', 'inbound', 'neutral', 3],
  ['story-fan-2', 'Sam Carter', 'That made my whole week, thank you!', 'inbound', 'positive', 1],
  ['story-fan-3', 'Jordan Blake', 'Sent it just now, let me know what you think.', 'outbound', 'positive', 0],
  ['story-fan-4', 'Alex Kim', 'Still waiting on a reply about the custom request.', 'inbound', 'negative', 2],
  ['story-fan-5', 'Casey Reed', 'Morning! Are you live later?', 'inbound', 'neutral', 0],
] as const;

function conversations(complete: boolean): ConversationSummary[] {
  return FANS.map(([id, name, text, direction, sentiment, unread], index) => {
    const sentAt = new Date(Date.parse(AS_OF) - (index * 47 + 6) * 60_000).toISOString();
    return {
      conversation_id: `${id}-conversation`,
      platform_user_id: id,
      display_name: name,
      unread_count: unread,
      last_message_at: sentAt,
      latest_message: { message_id: `${id}-latest`, text, sent_at: sentAt, direction, sentiment },
      coverage: {
        status: complete ? 'complete' : 'partial',
        boundary: complete ? 'history_start' : null,
        earliest_available_at: '2026-01-04T09:00:00.000Z',
        latest_acquired_at: AS_OF,
        data_as_of: AS_OF,
        reason_code: null,
      },
    };
  });
}

function snapshot(journey: Exclude<StoryJourneyName, 'loading'>): StateSnapshotPayload {
  const fresh = journey === 'fresh';
  const partial = journey === 'syncing';
  const counts = fresh
    ? [0, 0, 0, 0]
    : partial
      ? [86, 3214, 2140, 1074]
      : [248, 18406, 11972, 6434];
  return {
    creator_account_id: ACCOUNT,
    view_revision: 42,
    generated_at: AS_OF,
    conversations: fresh ? [] : conversations(!partial),
    analytics: {
      total_conversations: metric(counts[0], partial || fresh),
      total_messages: metric(counts[1], partial || fresh),
      inbound_messages: metric(counts[2], partial || fresh),
      outbound_messages: metric(counts[3], partial || fresh),
    },
    coverage: {
      status: fresh ? 'unknown' : partial ? 'partial' : 'complete',
      phase: fresh ? 'not_started' : partial ? 'backfilling' : 'complete',
      generation_id: fresh ? null : '90000000-0000-4000-8000-000000000001',
      as_of: fresh ? null : AS_OF,
      discovered_conversations: fresh ? null : 248,
      complete_conversations: fresh ? 0 : partial ? 86 : 248,
      complete_as_of: partial || fresh ? null : AS_OF,
      reason: null,
    },
    projection: {
      status: 'current',
      canonical_revision: 42,
      projected_revision: 42,
      projected_at: AS_OF,
      reason: null,
    },
    live_freshness: {
      status: fresh ? 'unknown' : 'current',
      last_observed_at: fresh ? null : AS_OF,
      last_committed_at: fresh ? null : AS_OF,
      expires_at: fresh ? null : '2026-06-30T12:02:00.000Z',
      pending_count: fresh ? null : 0,
      reason: null,
    },
  };
}

const connectedAgent: AgentStatePayload = {
  creator_account_id: ACCOUNT,
  status: 'connected',
  agent_installation_id: '90000000-0000-4000-8000-000000000002',
  connection_id: '90000000-0000-4000-8000-000000000003',
  required_config_revision: 'story-r1',
  applied_config_revision: 'story-r1',
  required_history_settings_revision: 1,
  applied_history_settings_revision: 1,
  last_heartbeat_at: AS_OF,
  degraded_reason: null,
};

function seedStores(journey: StoryJourneyName) {
  useUserStore.getState().actions.setUserRole('creator-ceo');
  bridgeTransportStore.reset();
  bridgeTransportStore.bindAccount(ACCOUNT);
  if (journey === 'loading') return;
  bridgeTransportStore.acceptSession({
    connection_id: '90000000-0000-4000-8000-000000000004',
    bridge_session_id: '90000000-0000-4000-8000-000000000005',
    creator_account_id: ACCOUNT,
    negotiated_protocol_version: '2',
    server_version: 'story',
  });
  bridgeTransportStore.applySnapshot(snapshot(journey));
  bridgeTransportStore.setSystem({
    creator_account_id: ACCOUNT,
    processing_mode: 'realtime',
    readiness: 'ready',
    updated_at: AS_OF,
    detail: null,
  });
  if (journey !== 'fresh') bridgeTransportStore.setAgent(connectedAgent);
}

function historySettings(journey: StoryJourneyName): HistorySettings {
  const on = journey === 'syncing' || journey === 'populated';
  return {
    creator_account_id: ACCOUNT,
    settings_revision: 1,
    consent_policy_version: 'history-consent-v1',
    consent_revision: on ? 'story-consent' : null,
    authorized_platform_creator_id: on ? 'story-creator' : null,
    desired_state: on ? 'running' : 'not_started',
    effective_state: on ? 'running' : 'not_applied',
    effective_config_revision: on ? 'story-r1' : null,
    recent_window_days: 30,
    page_size: 50,
    pages_per_wake: 5,
    request_interval_ms: 1000,
    retry_limit: 3,
    updated_at: AS_OF,
  };
}

function historyApi(journey: StoryJourneyName): HistorySettingsApi {
  const settings = historySettings(journey);
  return {
    get: async () => settings,
    update: async () => ({ ...settings, desired_state: 'running', effective_state: 'running' }),
    revoke: async () => ({ ...settings, desired_state: 'revoked', effective_state: 'revoked' }),
  };
}

function pin(): CompanionPairingStatus {
  return {
    pairing_id: 'S'.repeat(43),
    creator_account_id: ACCOUNT,
    generation: 1,
    version: 3,
    state: 'admitted',
    expires_at: '2026-06-30T12:10:00.000Z',
    comparison_code: null,
    agent_identity_thumbprint: 'T'.repeat(43),
  };
}

function pairingApi(journey: StoryJourneyName): CompanionPairingApi {
  const pins = journey === 'fresh' ? [] : [pin()];
  const open = {
    ...pin(),
    state: 'open' as const,
    version: 0,
    comparison_code: null,
    agent_identity_thumbprint: null,
  };
  const awaiting = {
    ...open,
    state: 'awaiting_confirmation' as const,
    version: 1,
    comparison_code: '482731',
    agent_identity_thumbprint: 'T'.repeat(43),
  };
  return {
    pins: async () => pins,
    open: async () => open,
    get: async () => awaiting,
    change: async (_pairingId, action) => {
      if (action === 'confirm') bridgeTransportStore.setAgent(connectedAgent);
      return action === 'confirm'
        ? { ...awaiting, state: 'confirmed' as const, version: 2 }
        : { ...open, state: 'cancelled' as const, version: 2 };
    },
    revoke: async () => ({ ...pin(), state: 'revoked' as const }),
  };
}

function activationApi(journey: StoryJourneyName): CapabilityLicenseApi {
  const active = journey === 'populated';
  return {
    readiness: async () => ({
      schema: 'ofca-analysis-readiness/v1',
      commercial_authority: active ? 'active' : 'required',
      analysis_admission: active ? 'admitted' : 'blocked',
    }),
    redeem: async () => ({ state: 'checking' }),
  };
}

function vaultApi(journey: StoryJourneyName): CreatorVaultApi {
  const status: CreatorVaultStatus = {
    creator_account_id: ACCOUNT,
    policy: journey === 'populated'
      ? { enabled: true, policy_type: 'finite', finite_horizon_days: 365, revision: 2 }
      : { enabled: false, policy_type: 'disabled', finite_horizon_days: null, revision: 0 },
    capabilities: {
      finite_retention: true,
      indefinite_retention: true,
      deletion_scopes: ['all'],
      unlink_archive_treatments: ['preserve'],
      export: true,
    },
  };
  return {
    get: async () => status,
    command: async (command) => ({
      action: command.action,
      status,
      deletion_revision: 1,
      deletion_operation: null,
      unlink_archive_treatment: null,
    }),
    exportDocument: async () => {
      throw new Error('Export is not available in the story harness.');
    },
  };
}

const storyMessageApi: MessageApi = {
  async getPage({ conversationId }): Promise<MessagePageResponse> {
    const fan = FANS.find(([id]) => `${id}-conversation` === conversationId) ?? FANS[0];
    const base = Date.parse(AS_OF);
    const lines: Array<['inbound' | 'outbound', string]> = [
      ['inbound', 'Hey! Loved the post from this morning.'],
      ['outbound', 'Thank you so much, glad you liked it!'],
      ['inbound', 'Any chance of a longer version this week?'],
      ['outbound', 'Working on it now, should be ready by Friday.'],
      [fan[3], fan[2]],
    ];
    return {
      creator_account_id: ACCOUNT,
      conversation_id: conversationId,
      projection_generation: 'story-generation',
      read_revision: 42,
      generated_at: AS_OF,
      items: lines.map(([direction, text], index) => ({
        message_id: `${conversationId}-${index}`,
        text,
        sent_at: new Date(base - (lines.length - index) * 9 * 60_000).toISOString(),
        direction,
        sentiment: index === lines.length - 1 ? fan[4] : 'neutral',
      })),
      older_cursor: null,
      has_older_stored_items: false,
      conversation_coverage: conversations(true)[0].coverage,
      projection: snapshot('populated').projection,
    };
  },
};

/** Rejects like a closed browser passkey prompt, so the failure state is reachable. */
const storyPasskeyApi: WebAuthnApi = {
  enroll: async () => {
    throw Object.assign(new Error('Story passkey prompt closed.'), { name: 'NotAllowedError' });
  },
  login: async () => {
    throw Object.assign(new Error('Story passkey prompt closed.'), { name: 'NotAllowedError' });
  },
};

const WORKSPACE_PATHS: Record<Exclude<StoryWorkspaceName, 'passkey'>, string> = {
  home: '/',
  analytics: '/analytics',
  inbox: '/inbox',
  settings: '/settings',
};

export function StoryWorkspace({
  analyticsState = storyAnalyticsState('model'),
  journey,
  workspace,
}: {
  analyticsState?: AnalyticsReadState;
  journey: StoryJourneyName;
  workspace: StoryWorkspaceName;
}) {
  if (workspace === 'passkey') {
    return <WebAuthnAccessView api={storyPasskeyApi} onAuthenticated={() => undefined} />;
  }
  seedStores(workspace === 'analytics' ? 'populated' : journey);
  return (
    <MemoryRouter initialEntries={[WORKSPACE_PATHS[workspace]]}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<CreatorDashboardView activationApi={activationApi(journey)} />} />
          <Route
            path="analytics"
            element={(
              <AnalyticsPresentation
                state={analyticsState}
                dateRange={storyDateRange}
                onDateRangeChange={() => undefined}
              />
            )}
          />
          <Route path="inbox" element={<OperatorInboxView messageApi={storyMessageApi} />} />
          <Route
            path="settings"
            element={(
              <SettingsWithVaultView
                activationApi={activationApi(journey)}
                historyApi={historyApi(journey)}
                pairingApi={pairingApi(journey)}
                vaultApi={vaultApi(journey)}
              />
            )}
          />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}
