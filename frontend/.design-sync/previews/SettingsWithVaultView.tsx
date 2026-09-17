import {
  Box,
  SettingsWithVaultView,
  seedPreviewShellStore,
} from 'onlyfans-analytics-frontend';

import type { CapabilityLicenseApi } from '../../src/services/capabilityLicenseApi';
import type {
  CompanionPairingApi,
  CompanionPairingStatus,
} from '../../src/services/companionPairingApi';
import type {
  CreatorVaultApi,
  CreatorVaultStatus,
} from '../../src/services/creatorVaultApi';
import type { HistorySettingsApi } from '../../src/services/historySettingsApi';

const mockHistoryApi: HistorySettingsApi = {
  get: async () => ({
    creator_account_id: 'creator-preview',
    settings_revision: 5,
    consent_policy_version: 'history-consent-v1',
    consent_revision: 'rev-5',
    authorized_platform_creator_id: 'platform-creator-bailey',
    desired_state: 'running',
    effective_state: 'running',
    effective_config_revision: 'cfg-preview-5',
    recent_window_days: 30,
    page_size: 50,
    pages_per_wake: 5,
    request_interval_ms: 1000,
    retry_limit: 3,
    updated_at: '2026-07-18T12:00:00.000Z',
  }),
  update: async (_rev, input) => ({
    creator_account_id: 'creator-preview',
    settings_revision: 6,
    consent_policy_version: 'history-consent-v1',
    consent_revision: 'rev-6',
    authorized_platform_creator_id: 'platform-creator-bailey',
    desired_state: input.desired_state,
    effective_state: input.desired_state === 'running' ? 'running' : 'paused',
    effective_config_revision: 'cfg-preview-6',
    recent_window_days: input.recent_window_days,
    page_size: input.page_size,
    pages_per_wake: input.pages_per_wake,
    request_interval_ms: input.request_interval_ms,
    retry_limit: input.retry_limit,
    updated_at: '2026-07-18T12:00:00.000Z',
  }),
  revoke: async () => ({
    creator_account_id: 'creator-preview',
    settings_revision: 7,
    consent_policy_version: 'history-consent-v1',
    consent_revision: null,
    authorized_platform_creator_id: null,
    desired_state: 'revoked',
    effective_state: 'revoked',
    effective_config_revision: null,
    recent_window_days: 30,
    page_size: 50,
    pages_per_wake: 5,
    request_interval_ms: 1000,
    retry_limit: 3,
    updated_at: '2026-07-18T12:00:00.000Z',
  }),
};

const mockVaultStatus: CreatorVaultStatus = {
  creator_account_id: 'creator-preview',
  policy: {
    enabled: true,
    policy_type: 'finite',
    finite_horizon_days: 365,
    revision: 3,
  },
  capabilities: {
    finite_retention: true,
    indefinite_retention: true,
    deletion_scopes: ['message', 'conversation', 'participant', 'all'],
    unlink_archive_treatments: ['preserve', 'delete'],
    export: true,
  },
  deletion_operation: null,
};

const mockVaultApi: CreatorVaultApi = {
  get: async () => mockVaultStatus,
  command: async (command) => ({
    action: command.action,
    status: {
      ...mockVaultStatus,
      policy: {
        ...mockVaultStatus.policy,
        enabled: command.action !== 'disable',
      },
    },
    deletion_revision: 1,
    deletion_operation: null,
    unlink_archive_treatment: command.unlink_archive_treatment ?? null,
  }),
  retryDeletion: async (operationId) => ({
    operation_id: operationId,
    status: 'complete',
    deletion_revision: 2,
  }),
  exportDocument: async () => ({
    manifest: {
      export_type: 'creator_vault',
      content: {
        conversation_count: 12,
        message_count: 48,
        sha256: 'mock-vault-sha256',
      },
      copy_domains: {
        managed_recovery: {
          inspection_complete: true,
          copies_may_remain: false,
        },
        this_export_after_delivery: {
          managed_by_product: false,
          observable_by_product: false,
          managed_vault_deletion_applies: false,
        },
      },
    },
    conversations: [],
    messages: [],
  }),
};

function pairingStatus(
  overrides: Partial<CompanionPairingStatus> = {},
): CompanionPairingStatus {
  return {
    pairing_id: 'A'.repeat(43),
    creator_account_id: 'preview-creator',
    generation: 1,
    version: 0,
    state: 'open',
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    comparison_code: null,
    agent_identity_thumbprint: null,
    ...overrides,
  };
}

const mockPairingApi: CompanionPairingApi = {
  pins: async () => [],
  revoke: async (_pairingId, version) => pairingStatus({ state: 'revoked', version: version + 1 }),
  open: async () => pairingStatus(),
  get: async () => pairingStatus({
    state: 'awaiting_confirmation',
    version: 1,
    comparison_code: '482913',
    agent_identity_thumbprint: 'B'.repeat(43),
  }),
  change: async (_pairingId, action, version) => pairingStatus({
    state: action === 'confirm' ? 'admitted' : action === 'decline' ? 'declined' : 'cancelled',
    version: version + 1,
  }),
};

const mockActivationApi: CapabilityLicenseApi = {
  readiness: async () => ({
    schema: 'ofca-analysis-readiness/v1',
    commercial_authority: 'required',
    analysis_admission: 'blocked',
  }),
  redeem: async () => ({ state: 'checking' }),
};

export function CompositeSettings() {
  seedPreviewShellStore();
  return (
    <Box sx={{ bgcolor: 'background.default', minHeight: 760, p: 3 }}>
      <SettingsWithVaultView
        historyApi={mockHistoryApi}
        pairingApi={mockPairingApi}
        activationApi={mockActivationApi}
        vaultApi={mockVaultApi}
      />
    </Box>
  );
}
