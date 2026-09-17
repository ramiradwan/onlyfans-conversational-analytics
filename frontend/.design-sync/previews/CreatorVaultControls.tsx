import {
  Box,
  CreatorVaultControls,
  previewNoop,
  useUserStore,
} from 'onlyfans-analytics-frontend';

import type {
  CreatorVaultApi,
  CreatorVaultStatus,
} from '../../src/services/creatorVaultApi';

const baseCapabilities: CreatorVaultStatus['capabilities'] = {
  finite_retention: true,
  indefinite_retention: true,
  deletion_scopes: ['message', 'conversation', 'participant', 'all'],
  unlink_archive_treatments: ['preserve', 'delete'],
  export: true,
};

function createMockVaultApi(status: CreatorVaultStatus): CreatorVaultApi {
  return {
    get: async () => status,
    command: async (command) => ({
      action: command.action,
      status: {
        ...status,
        policy: {
          ...status.policy,
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
          conversation_count: 4,
          message_count: 8,
          sha256: 'mock-sha256',
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
}

const finiteRetentionApi = createMockVaultApi({
  creator_account_id: 'creator-preview',
  policy: {
    enabled: true,
    policy_type: 'finite',
    finite_horizon_days: 365,
    revision: 1,
  },
  capabilities: baseCapabilities,
  deletion_operation: null,
});

const disabledApi = createMockVaultApi({
  creator_account_id: 'creator-preview',
  policy: {
    enabled: false,
    policy_type: 'disabled',
    finite_horizon_days: null,
    revision: 1,
  },
  capabilities: baseCapabilities,
  deletion_operation: null,
});

const incompleteDeletionApi = createMockVaultApi({
  creator_account_id: 'creator-preview',
  policy: {
    enabled: true,
    policy_type: 'finite',
    finite_horizon_days: 180,
    revision: 2,
  },
  capabilities: baseCapabilities,
  deletion_operation: {
    operation_id: 'op-incomplete-1',
    status: 'incomplete',
    deletion_revision: 2,
  },
});

export function FiniteRetention() {
  useUserStore.getState().actions.setUserRole('creator-ceo');
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 960, p: 2 }}>
      <CreatorVaultControls api={finiteRetentionApi} onDownload={previewNoop} />
    </Box>
  );
}

export function Disabled() {
  useUserStore.getState().actions.setUserRole('creator-ceo');
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 960, p: 2 }}>
      <CreatorVaultControls api={disabledApi} onDownload={previewNoop} />
    </Box>
  );
}

export function IncompleteDeletion() {
  useUserStore.getState().actions.setUserRole('creator-ceo');
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 960, p: 2 }}>
      <CreatorVaultControls api={incompleteDeletionApi} onDownload={previewNoop} />
    </Box>
  );
}
